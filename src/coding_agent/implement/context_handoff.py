from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from coding_agent.implement.exchange import ExchangeUnit, flatten
from coding_agent.implement.result_capping import ArtifactStore


CONTINUATION_SNAPSHOT_VERSION = 1
HANDOFF_FAILURE = "handoff-failure"
_FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """Use the same deterministic provider-neutral estimate as the prefix."""
    return max(1, len(text.encode("utf-8")) // 4)


@dataclass(frozen=True)
class ContextWindowConfig:
    """The deterministic context policy for one Pinned Model.

    The threshold is an estimate, not a provider usage claim. Overhead covers
    tool schemas and request framing; the safety margin leaves room for small
    provider-side differences. Once the next request would exceed the usable
    threshold, the current node is handed off to a fresh node.
    """

    threshold_tokens: int
    safety_margin_tokens: int = 1_000
    request_overhead_tokens: int = 1_000
    handoff_token_cap: int = 2_000

    def __post_init__(self) -> None:
        if self.threshold_tokens <= 0:
            raise ValueError("context-window threshold must be positive")
        if self.safety_margin_tokens < 0:
            raise ValueError("context-window safety margin cannot be negative")
        if self.request_overhead_tokens < 0:
            raise ValueError("context-window request overhead cannot be negative")
        if self.safety_margin_tokens + self.request_overhead_tokens >= self.threshold_tokens:
            raise ValueError("context-window overhead and safety margin must leave usable capacity")
        if self.handoff_token_cap <= 0:
            raise ValueError("handoff token cap must be positive")

    @property
    def usable_threshold(self) -> int:
        return self.threshold_tokens - self.safety_margin_tokens - self.request_overhead_tokens


ContextWindowTable = dict[str, ContextWindowConfig]


class HandoffFailure(Exception):
    """A handoff could not produce and durably retain a valid snapshot."""

    classification = HANDOFF_FAILURE


@dataclass(frozen=True)
class ContinuationSnapshot:
    """A redacted, provider-neutral continuation contract.

    It contains explicit repository facts only. Provider-specific assistant
    messages are deliberately absent, so a continuation cannot reconstruct
    opaque reasoning blocks from a previous node.
    """

    version: int
    attempt_id: str
    base_revision: str
    branch: str
    commit: str
    changed_files: tuple[str, ...]
    commands: tuple[dict[str, str], ...]
    validation_evidence: object
    unresolved_questions: tuple[str, ...]
    next_intended_action: str

    @classmethod
    def from_payload(
        cls, payload: object, *, redactions: Mapping[str, str] | None = None
    ) -> ContinuationSnapshot:
        if not isinstance(payload, Mapping):
            raise HandoffFailure("snapshot is not a JSON object")
        expected = {
            "version",
            "attempt_id",
            "base_revision",
            "branch",
            "commit",
            "changed_files",
            "commands",
            "validation_evidence",
            "unresolved_questions",
            "next_intended_action",
        }
        missing = sorted(field for field in expected if field not in payload)
        if missing:
            raise HandoffFailure(f"snapshot is missing required fields: {', '.join(missing)}")
        version = payload["version"]
        if version != CONTINUATION_SNAPSHOT_VERSION:
            raise HandoffFailure(f"unsupported continuation snapshot version: {version!r}")

        def required_string(name: str) -> str:
            value = payload[name]
            if not isinstance(value, str) or not value.strip():
                raise HandoffFailure(f"snapshot field {name!r} must be a non-empty string")
            return value

        changed_files = _string_tuple(payload["changed_files"], "changed_files")
        unresolved_questions = _string_tuple(payload["unresolved_questions"], "unresolved_questions")
        commands_value = payload["commands"]
        if not isinstance(commands_value, Sequence) or isinstance(commands_value, (str, bytes)):
            raise HandoffFailure("snapshot field 'commands' must be a list")
        commands: list[dict[str, str]] = []
        for index, command in enumerate(commands_value):
            if not isinstance(command, Mapping):
                raise HandoffFailure(f"snapshot command {index} must be an object")
            command_text = command.get("command")
            result_text = command.get("result")
            if not isinstance(command_text, str) or not isinstance(result_text, str):
                raise HandoffFailure(f"snapshot command {index} must contain command and result strings")
            commands.append({"command": command_text, "result": result_text})

        raw = {
            "version": version,
            "attempt_id": required_string("attempt_id"),
            "base_revision": required_string("base_revision"),
            "branch": required_string("branch"),
            "commit": required_string("commit"),
            "changed_files": list(changed_files),
            "commands": commands,
            "validation_evidence": payload["validation_evidence"],
            "unresolved_questions": list(unresolved_questions),
            "next_intended_action": required_string("next_intended_action"),
        }
        redacted_value = _redact_json(raw, redactions or {})
        if not isinstance(redacted_value, dict):
            raise HandoffFailure("redacted snapshot is not a JSON object")
        redacted = redacted_value
        return cls(
            version=cast(int, redacted["version"]),
            attempt_id=cast(str, redacted["attempt_id"]),
            base_revision=cast(str, redacted["base_revision"]),
            branch=cast(str, redacted["branch"]),
            commit=cast(str, redacted["commit"]),
            changed_files=tuple(cast(list[str], redacted["changed_files"])),
            commands=tuple(cast(list[dict[str, str]], redacted["commands"])),
            validation_evidence=redacted["validation_evidence"],
            unresolved_questions=tuple(cast(list[str], redacted["unresolved_questions"])),
            next_intended_action=cast(str, redacted["next_intended_action"]),
        )

    @classmethod
    def from_json(cls, encoded: str) -> ContinuationSnapshot:
        try:
            payload: object = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise HandoffFailure(f"stored snapshot is not valid JSON: {exc.msg}") from exc
        return cls.from_payload(payload)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "attempt_id": self.attempt_id,
            "base_revision": self.base_revision,
            "branch": self.branch,
            "commit": self.commit,
            "changed_files": list(self.changed_files),
            "commands": list(self.commands),
            "validation_evidence": self.validation_evidence,
            "unresolved_questions": list(self.unresolved_questions),
            "next_intended_action": self.next_intended_action,
        }

    def to_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return self.digest_for(self.to_json())

    @staticmethod
    def digest_for(encoded: str) -> str:
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HandoffFailure(f"snapshot field {name!r} must be a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise HandoffFailure(f"snapshot field {name!r} must be a list of strings")
    return tuple(cast(str, item) for item in value)


def _redact_json(value: object, secrets: Mapping[str, str]) -> object:
    if isinstance(value, str):
        redacted = value
        for name, secret in secrets.items():
            if secret:
                redacted = redacted.replace(secret, f"«redacted:{name}»")
        return redacted
    if isinstance(value, Mapping):
        return {str(key): _redact_json(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    return value


def snapshot_artifact_key(digest: str) -> str:
    """Stable artifact address used as the idempotency key for a snapshot."""
    return f"continuation-snapshots/{digest}.json"


def failure_artifact_key(attempt_id: str, sequence: int) -> str:
    safe_attempt = re.sub(r"[^A-Za-z0-9_.-]+", "-", attempt_id).strip("-") or "attempt"
    return f"continuation-snapshots/{safe_attempt}/handoff-failure-{sequence}.json"


def persist_snapshot(store: ArtifactStore, snapshot: ContinuationSnapshot) -> str:
    artifact_ref = snapshot_artifact_key(snapshot.digest)
    try:
        store.store(artifact_ref, snapshot.to_json())
        stored = store.read(artifact_ref)
    except Exception as exc:
        raise HandoffFailure(f"could not persist continuation snapshot: {exc}") from exc
    if ContinuationSnapshot.digest_for(stored) != snapshot.digest:
        raise HandoffFailure("persisted continuation snapshot digest did not verify")
    return artifact_ref


def load_recorded_snapshot(ledger: object, store: ArtifactStore, attempt_id: str) -> ContinuationSnapshot | None:
    """Load the last recorded snapshot, refusing a broken digest or artifact."""
    records_method = getattr(ledger, "records", None)
    if records_method is None:
        return None
    records = records_method(attempt_id)
    for record in reversed(records):
        if record.kind != "handoff":
            continue
        digest = record.payload.get("snapshot_digest")
        artifact_ref = record.artifact_ref
        if not isinstance(digest, str) or not isinstance(artifact_ref, str):
            raise HandoffFailure("handoff ledger record lacks a snapshot digest or artifact reference")
        try:
            encoded = store.read(artifact_ref)
        except Exception as exc:
            raise HandoffFailure(f"recorded continuation snapshot is unavailable: {exc}") from exc
        if ContinuationSnapshot.digest_for(encoded) != digest:
            raise HandoffFailure("recorded continuation snapshot digest does not match its artifact")
        snapshot = ContinuationSnapshot.from_json(encoded)
        if snapshot.digest != digest or snapshot.attempt_id != attempt_id:
            raise HandoffFailure("recorded continuation snapshot failed identity validation")
        return snapshot
    return None


def continuation_message(snapshot: ContinuationSnapshot) -> HumanMessage:
    """Render only explicit snapshot facts into the next work node."""
    return HumanMessage(content=f"# Continuation Snapshot\n\n{snapshot.to_json()}")


def estimate_context_tokens(
    opening_messages: Sequence[BaseMessage],
    history: Sequence[ExchangeUnit],
    tools: Sequence[BaseTool],
    *,
    continuation: ContinuationSnapshot | None = None,
) -> int:
    """Estimate the request including prefix, history, tools and snapshot."""
    tool_schemas = json.dumps(
        [tool.args for tool in tools], sort_keys=True, default=str, separators=(",", ":")
    )
    messages = [*opening_messages, *flatten(history)]
    if continuation is not None:
        messages.append(continuation_message(continuation))
    message_text = "".join(
        json.dumps(
            {
                "content": message.content,
                "tool_calls": getattr(message, "tool_calls", ()),
                "additional_kwargs": getattr(message, "additional_kwargs", {}),
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        for message in messages
    )
    return estimate_tokens(message_text + tool_schemas)


def response_text(content: object) -> str:
    """Extract model text while retaining no provider-specific blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(cast(str, block["text"]))
        if parts:
            return "\n".join(parts)
    return str(content)


def parse_snapshot_response(content: object, *, redactions: Mapping[str, str]) -> ContinuationSnapshot:
    text = response_text(content)
    fenced = _FENCED_JSON.match(text)
    candidate = fenced.group(1) if fenced else text
    try:
        payload: object = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise HandoffFailure(f"handoff response was not valid JSON: {exc.msg}") from exc
    return ContinuationSnapshot.from_payload(payload, redactions=redactions)
