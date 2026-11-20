"""The per-Attempt model transcript: every request and response, redacted.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (a claim return names
what failed; the transcript is what lets an operator see which check
returned a live claim) and §18 (registered secrets are replaced by
``«redacted:<name>»`` before anything is written), and §17 (every record
carries a ``conversation`` label; one monotonic sequence across the file).

The transcript wraps the Pinned Model at its port (``InvokableToolModel``),
so every conversation that goes through it is recorded with no change to
the step graph or the Sub-agent conversations. Each
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

Concurrency: every record is written under one per-file lock with one
monotonic ``seq`` across the whole file, so concurrent writers from several
threads can never interleave inside a record and the sequence never forks.

MVP row ids: L3-ERR-11, C-IMP-1.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.redaction import Redactable, registry_from

TRANSCRIPT_FILE_NAME = "transcript.jsonl"
"""The transcript's file name inside the Attempt's evidence directory."""

DEFAULT_CONVERSATION = "caller"
"""The conversation label where the caller names none.

Production step-workflow code always names one: each Step Execution forks
its own ``step/<step-id>/<n>`` label, each fresh node after a
Context-Window Handoff its own ``step/<step-id>/<n>/continuation/<k>``
label, each bounded handoff turn its own
``step/<step-id>/<n>/handoff/<k>`` label and each Sub-agent its own
``subagent:<call id>`` label (see :func:`work_conversation`,
:func:`handoff_turn_conversation` and :func:`subagent_conversation`). The
default only keeps direct ``TranscriptWriter.write`` calls — the unit-test
seam — inside the invariant that every record carries a label.
"""


def work_conversation(step_id: str, seq: int, node_index: int = 0) -> str:
    """The conversation label for one Step Execution's caller conversation.

    The Step Execution's own conversation is ``step/<step-id>/<n>`` where
    ``<n>`` is its sequence number; each fresh node after a Context-Window
    Handoff keeps its own label as
    ``step/<step-id>/<n>/continuation/<k>``, so an auditor can follow each
    conversation even when several interleave in one file.
    """
    if node_index <= 0:
        return f"step/{step_id}/{seq}"
    return f"step/{step_id}/{seq}/continuation/{node_index}"


def handoff_turn_conversation(step_id: str, seq: int, sequence: int) -> str:
    """The conversation label for one bounded handoff turn."""
    return f"step/{step_id}/{seq}/handoff/{sequence}"


def subagent_conversation(call_id: str) -> str:
    """The conversation label for one Sub-agent's conversation (contract §8)."""
    return f"subagent:{call_id}"


_WRITER_GUARD = threading.Lock()
_WRITER_LOCKS: dict[str, threading.Lock] = {}
_WRITER_SEQUENCES: dict[str, int] = {}


def _writer_key(path: Path) -> str:
    """One shared lock/sequence per transcript file."""
    return str(path.absolute())


def _initial_sequence(path: Path) -> int:
    """The highest ``seq`` already in ``path``, or zero where none."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    highest = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            seq = record.get("seq")
            if isinstance(seq, bool):
                continue
            if isinstance(seq, int) and seq > highest:
                highest = seq
    return highest


class TranscriptWriter:
    """Appends redacted JSON records to one transcript file.

    ``redactions`` is any form the redaction registry accepts (a
    :class:`SecretRegistry`, a name-to-value mapping, a bare credential, or
    ``None``). Each value is replaced by ``«redacted:<name>»`` in the
    serialized line — the one registry does the replacing, so there is no
    second redaction path (§18, L3-ERR-11).

    Every record carries a ``conversation`` label and one monotonic ``seq``
    across the whole file (§17). Records are written under one per-file
    lock shared by every writer for that path, so concurrent writers from
    several threads each emit whole lines with distinct sequence numbers.
    """

    def __init__(
        self,
        path: Path,
        redactions: Redactable = None,
        *,
        default_conversation: str = DEFAULT_CONVERSATION,
    ) -> None:
        self._path = path
        self._registry = registry_from(redactions)
        self._default_conversation = default_conversation
        key = _writer_key(path)
        with _WRITER_GUARD:
            if key not in _WRITER_LOCKS:
                _WRITER_LOCKS[key] = threading.Lock()
            if key not in _WRITER_SEQUENCES:
                _WRITER_SEQUENCES[key] = _initial_sequence(path)
            self._key = key

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: Mapping[str, object]) -> None:
        mutable: dict[str, object] = dict(record)
        if "conversation" not in mutable or not isinstance(
            mutable["conversation"], str
        ):
            mutable["conversation"] = self._default_conversation
        lock = _WRITER_LOCKS[self._key]
        with lock:
            with _WRITER_GUARD:
                _WRITER_SEQUENCES[self._key] += 1
                sequence = _WRITER_SEQUENCES[self._key]
            stamped = {
                **mutable,
                "seq": sequence,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            # ``seq`` and ``at`` are the writer's: a caller record never
            # overrides them.
            line = self._registry.redact(
                json.dumps(stamped, default=str, ensure_ascii=False)
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


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


_TURN_LOCK = threading.Lock()
_TURN_COUNTS: dict[tuple[str, str], int] = {}
"""Per-conversation turn numbers for the container log (writer key, label)."""


def _next_turn(writer: TranscriptWriter, conversation: str) -> int:
    """The next per-conversation turn number for the container log."""
    key = (writer._key, conversation)
    with _TURN_LOCK:
        _TURN_COUNTS[key] = _TURN_COUNTS.get(key, 0) + 1
        return _TURN_COUNTS[key]


def _tool_names_from_response(response: BaseMessage) -> list[str]:
    """The tool-call names in a model response (names only, never arguments)."""
    calls = getattr(response, "tool_calls", None)
    names: list[str] = []
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
        for call in calls:
            if isinstance(call, Mapping):
                name = call.get("name")
            else:
                name = getattr(call, "name", None)
            if isinstance(name, str) and name:
                names.append(name)
    return names


def _usage_suffix(response: BaseMessage) -> str:
    """The ``(in X / out Y)`` usage suffix, or ``""`` without usage metadata."""
    from coding_agent.operator.runlog import format_tokens

    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        return ""
    raw_in = usage.get("input_tokens")
    raw_out = usage.get("output_tokens")
    parts: list[str] = []
    if isinstance(raw_in, bool):
        raw_in = None
    if isinstance(raw_out, bool):
        raw_out = None
    if isinstance(raw_in, (int, float)):
        parts.append(f"in {format_tokens(int(raw_in))}")
    if isinstance(raw_out, (int, float)):
        parts.append(f"out {format_tokens(int(raw_out))}")
    if not parts:
        return ""
    return " (" + " / ".join(parts) + ")"


def _print_turn_response(
    writer: TranscriptWriter, conversation: str, response: BaseMessage
) -> None:
    """Print one container-log line for a model response (best-effort)."""
    try:
        from coding_agent.operator.runlog import log_turns_enabled, print_line

        if not log_turns_enabled():
            return
        turn = _next_turn(writer, conversation)
        names = _tool_names_from_response(response)
        calls = ", ".join(names) if names else "no tool calls"
        line = f"run: [{conversation}] turn {turn}: {calls}{_usage_suffix(response)}"
        print_line(writer._registry.redact(line))
    except Exception:
        pass


def _print_turn_error(
    writer: TranscriptWriter, conversation: str, exc_type: str
) -> None:
    """Print one container-log line for a failed model call (best-effort)."""
    try:
        from coding_agent.operator.runlog import log_turns_enabled, print_line

        if not log_turns_enabled():
            return
        turn = _next_turn(writer, conversation)
        line = f"run: [{conversation}] turn {turn} failed: {exc_type}"
        print_line(writer._registry.redact(line))
    except Exception:
        pass


class TranscriptModel:
    """An ``InvokableToolModel`` that records each request and response.

    ``bind_tools`` binds the wrapped model and returns a recording wrapper
    around the bound one, so every conversation built on it is recorded.
    The response is returned exactly as the wrapped model returned it, never
    rebuilt (ADR 0010).

    ``conversation`` labels this wrapper's caller conversation (§17). Use
    :meth:`with_conversation` to fork one label per work node or Sub-agent:
    the fork shares the writer — hence the single file sequence —
    and records under its own label, so interleaved conversations stay
    auditable. ``bind_tools`` preserves the label.
    """

    def __init__(
        self,
        inner: InvokableToolModel,
        writer: TranscriptWriter,
        *,
        tool_names: tuple[str, ...] = (),
        tool_choice: str | None = None,
        conversation: str = DEFAULT_CONVERSATION,
        first_request_extra: Mapping[str, object] | None = None,
    ) -> None:
        self._inner = inner
        self._writer = writer
        self._tool_names = tool_names
        self._tool_choice = tool_choice
        self._conversation = conversation
        self._first_request_extra: dict[str, object] | None = (
            dict(first_request_extra) if first_request_extra is not None else None
        )

    @property
    def conversation(self) -> str:
        """This wrapper's conversation label (§17)."""
        return self._conversation

    def with_conversation(
        self,
        conversation: str,
        *,
        first_request_extra: Mapping[str, object] | None = None,
    ) -> TranscriptModel:
        """Fork this wrapper under ``conversation``, sharing the writer.

        The fork shares the wrapped model target and the file writer, so
        records keep one monotonic sequence across the file while carrying
        distinct labels per conversation. ``first_request_extra`` is merged
        into the fork's first request record only — the Sub-agent fork
        carries its ``description`` there — and is consumed by that request.
        """
        merged: dict[str, object] = {}
        if self._first_request_extra is not None:
            merged.update(self._first_request_extra)
        if first_request_extra is not None:
            merged.update(first_request_extra)
        return TranscriptModel(
            self._inner,
            self._writer,
            tool_names=self._tool_names,
            tool_choice=self._tool_choice,
            conversation=conversation,
            first_request_extra=merged or None,
        )

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
            conversation=self._conversation,
            first_request_extra=self._first_request_extra,
        )

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        request: dict[str, Any] = {
            "kind": "request",
            "conversation": self._conversation,
            "tools": list(self._tool_names),
            "tool_choice": self._tool_choice,
        }
        if "max_tokens" in kwargs:
            request["max_tokens"] = kwargs["max_tokens"]
        if self._first_request_extra is not None:
            request.update(self._first_request_extra)
            self._first_request_extra = None
        if isinstance(input, Sequence) and not isinstance(input, str):
            messages = [m for m in input if isinstance(m, BaseMessage)]
            request["message_count"] = len(messages)
            request["new_messages"] = [serialize_message(m) for m in _new_messages(messages)]
        else:
            request["input"] = input
        self._writer.write(request)
        try:
            inner_invoke = cast(Callable[..., BaseMessage], self._inner.invoke)
            response = (
                inner_invoke(input, **kwargs) if kwargs else self._inner.invoke(input)
            )
        except Exception as exc:
            self._writer.write(
                {
                    "kind": "error",
                    "conversation": self._conversation,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            _print_turn_error(self._writer, self._conversation, type(exc).__name__)
            raise
        self._writer.write(
            {"kind": "response", "conversation": self._conversation, "message": serialize_message(response)}
        )
        _print_turn_response(self._writer, self._conversation, response)
        return response


def fork_conversation(
    model: InvokableToolModel, conversation: str
) -> InvokableToolModel:
    """Fork ``model`` under ``conversation`` where it records a transcript.

    A :class:`TranscriptModel` shares its file writer with the fork, so the
    single sequence across the file is kept while the new conversation gets
    its own label (§17). Any other model is returned unchanged: unit-test
    doubles record nothing either way.
    """
    if isinstance(model, TranscriptModel):
        return model.with_conversation(conversation)
    return model


def fork_subagent_conversation(
    model: InvokableToolModel, call_id: str, description: str
) -> InvokableToolModel:
    """Fork ``model`` under ``subagent:<call id>`` carrying ``description``.

    The fork records under the Sub-agent's conversation label (§17), and its
    first request record carries the call's ``description`` (contract §8). Any
    other model is returned unchanged: unit-test doubles record nothing
    either way.
    """
    if isinstance(model, TranscriptModel):
        return model.with_conversation(
            subagent_conversation(call_id),
            first_request_extra={"description": description},
        )
    return model
