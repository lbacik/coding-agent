"""Per-call ``tool_call`` / ``tool_result`` / ``model_output`` artifacts (contract 1.2).

Contract: ``agent-operator-contract`` §7.6 (the ManifestEntry link fields),
§11.1 (consumers pair only by ``call_id`` and order by ``at``) and the
§16 coding-agent mapping at ``v1.2.0``: every Tool Invocation recorded in
the Transcript is archived live as a ``tool_call`` artifact (the call's
redacted ``args``, ``application/json``) and a ``tool_result`` artifact
(the tool message content), sharing ``call_id``, plus a ``model_output``
artifact (``text/plain``) for every assistant response with text. Entries
carry ``at``, ``conversation`` and ``origin`` (``main`` for step
conversations, ``subagent`` for ``subagent:`` labels); calls and results
also carry ``tool``. A call without a result is allowed in a Sealed
archive. ``transcript.jsonl`` is unchanged.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, get_args

import pytest
from agent_operator_contract import documents as contract_documents

from coding_agent import agentctl
from coding_agent.conformance import CONTRACT_PIN, CONTRACT_VERSION
from coding_agent.operator import archive as archive_mod
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator import contract as operator_contract
from coding_agent.runtime import transcript as transcript_module
from coding_agent.runtime.transcript import (
    TranscriptWriter,
    subagent_conversation,
    work_conversation,
)

MAIN_CONVERSATION = work_conversation("implement", 1)
SUB_CONVERSATION = subagent_conversation("call-sub-1")


def _response_record(
    conversation: str,
    content: object,
    tool_calls: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"type": "ai", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"kind": "response", "conversation": conversation, "message": message}


def _request_record(
    conversation: str, tool_contents: list[tuple[str, object]]
) -> dict[str, Any]:
    return {
        "kind": "request",
        "conversation": conversation,
        "new_messages": [
            {"type": "tool", "content": content, "tool_call_id": call_id}
            for call_id, content in tool_contents
        ],
    }


def _write_main_flow(writer: TranscriptWriter) -> None:
    """Two calls in one response, both results, then a text response."""
    writer.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [
                {"name": "run_command", "id": "call_1", "args": {"cmd": "ls"}},
                {"name": "read", "id": "call_2", "args": {"path": "a.txt"}},
            ],
        )
    )
    writer.write(
        _request_record(
            MAIN_CONVERSATION, [("call_1", "total 0"), ("call_2", "hello")]
        )
    )
    writer.write(_response_record(MAIN_CONVERSATION, "done"))


def _entries_by_path(archive_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        entry.path: entry.to_dict()
        for entry in archive_mod.build_manifest(archive_dir)
    }


def _validate_entries(entries: dict[str, dict[str, Any]]) -> None:
    for path, entry in entries.items():
        assert contract_documents.validate_manifest_entry(entry) == [], path


def test_two_calls_and_text_response_archive_paired_artifacts(tmp_path: Path) -> None:
    """Two calls, two results paired by ``call_id``, plus the text output."""
    archive_dir = tmp_path / "archive"
    _write_main_flow(TranscriptWriter(archive_dir / "transcript.jsonl"))

    entries = _entries_by_path(archive_dir)
    _validate_entries(entries)
    assert contract_documents._validate_manifest_links(
        list(entries.values()), "manifest", sealed=True
    ) == []

    for call_id, tool, args in (
        ("call_1", "run_command", {"cmd": "ls"}),
        ("call_2", "read", {"path": "a.txt"}),
    ):
        call = entries[f"tool_calls/{call_id}.json"]
        result = entries[f"tool_results/{call_id}.txt"]
        assert call["kind"] == "tool_call"
        assert result["kind"] == "tool_result"
        assert call["call_id"] == result["call_id"] == call_id
        assert call["tool"] == result["tool"] == tool
        assert call["conversation"] == result["conversation"] == MAIN_CONVERSATION
        assert call["origin"] == result["origin"] == "main"
        assert str(call["at"]).endswith("Z")
        assert str(result["at"]).endswith("Z")
        assert json.loads(
            (archive_dir / call["path"]).read_text(encoding="utf-8")
        ) == args
        assert call["media_type"] == "application/json"
        assert result["media_type"] == "text/plain"

    output_paths = [
        path
        for path, entry in entries.items()
        if entry["kind"] == "model_output"
    ]
    assert output_paths == ["model_outputs/3.txt"]
    output = entries["model_outputs/3.txt"]
    assert output["conversation"] == MAIN_CONVERSATION
    assert output["origin"] == "main"
    assert str(output["at"]).endswith("Z")
    assert "call_id" not in output
    assert "tool" not in output
    assert (archive_dir / output["path"]).read_text(encoding="utf-8") == "done"

    contents = (archive_dir / "tool_results/call_1.txt").read_text(encoding="utf-8")
    assert contents == "total 0"


def test_empty_response_text_writes_no_model_output(tmp_path: Path) -> None:
    """A response without text leaves no ``model_output`` artifact."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(_response_record(MAIN_CONVERSATION, ""))

    entries = _entries_by_path(archive_dir)
    assert [e for e in entries.values() if e["kind"] == "model_output"] == []


def test_subagent_conversation_uses_subagent_origin(tmp_path: Path) -> None:
    """A Sub-agent's call and result carry its label and ``subagent`` origin."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(
        _response_record(
            SUB_CONVERSATION,
            "",
            [{"name": "read", "id": "call_9", "args": {"path": "b.txt"}}],
        )
    )
    writer.write(_request_record(SUB_CONVERSATION, [("call_9", "body")]))

    entries = _entries_by_path(archive_dir)
    _validate_entries(entries)
    call = entries["tool_calls/call_9.json"]
    result = entries["tool_results/call_9.txt"]
    assert call["conversation"] == result["conversation"] == SUB_CONVERSATION
    assert call["origin"] == result["origin"] == "subagent"
    assert call["tool"] == result["tool"] == "read"


def test_tool_call_visible_before_its_result(tmp_path: Path) -> None:
    """The unsealed manifest lists a call as soon as its response is recorded."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [{"name": "run_command", "id": "call_1", "args": {"cmd": "ls"}}],
        )
    )

    early = _entries_by_path(archive_dir)
    assert early["tool_calls/call_1.json"]["call_id"] == "call_1"
    assert "tool_results/call_1.txt" not in early
    assert contract_documents._validate_manifest_links(
        list(early.values()), "manifest", sealed=False
    ) == []

    writer.write(_request_record(MAIN_CONVERSATION, [("call_1", "total 0")]))
    late = _entries_by_path(archive_dir)
    assert late["tool_results/call_1.txt"]["call_id"] == "call_1"


def test_sealed_manifest_allows_call_without_result(tmp_path: Path) -> None:
    """A conversation's final call seals with no result and still validates."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [{"name": "run_command", "id": "call_last", "args": {"cmd": "exit"}}],
        )
    )

    manifest = [
        entry.to_dict() for entry in archive_mod.build_manifest(archive_dir)
    ]
    archive_mod.write_sealed_manifest_atomically(
        archive_dir,
        archive_mod.build_sealed_document(
            "#7/1", archive_mod.build_manifest(archive_dir), "2026-01-01T00:00:00Z"
        ),
    )
    assert manifest and any(
        entry.get("kind") == "tool_call"
        and entry.get("call_id") == "call_last"
        for entry in manifest
    )
    assert not any(entry.get("kind") == "tool_result" for entry in manifest)
    for entry in manifest:
        assert contract_documents.validate_manifest_entry(entry) == []
    assert contract_documents._validate_manifest_links(
        manifest, "manifest", sealed=True
    ) == []


def test_nothing_is_written_into_a_sealed_archive(tmp_path: Path) -> None:
    """After the seal, recording keeps the archive byte-identical."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [{"name": "run_command", "id": "call_1", "args": {"cmd": "ls"}}],
        )
    )
    archive_mod.write_sealed_manifest_atomically(
        archive_dir,
        archive_mod.build_sealed_document(
            "#7/1", archive_mod.build_manifest(archive_dir), "2026-01-01T00:00:00Z"
        ),
    )
    before = {
        path: (archive_dir / path).read_bytes()
        for path in [
            str(p.relative_to(archive_dir))
            for p in sorted(archive_dir.rglob("*"))
            if p.is_file()
        ]
    }

    writer.write(_response_record(MAIN_CONVERSATION, "late"))

    after = {
        path: (archive_dir / path).read_bytes()
        for path in [
            str(p.relative_to(archive_dir))
            for p in sorted(archive_dir.rglob("*"))
            if p.is_file()
        ]
    }
    # The transcript line itself still lands (the writer owns the file);
    # no artifact file and no index row may be added to a Sealed archive.
    assert set(after) == set(before)
    assert before["tool-invocations.jsonl"] == after["tool-invocations.jsonl"]


def test_rerecord_after_restart_adds_no_duplicates(tmp_path: Path) -> None:
    """A retried request re-records its tool messages without new artifacts."""
    transcript = tmp_path / "transcript.jsonl"
    first = TranscriptWriter(transcript)
    first.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [{"name": "run_command", "id": "call_1", "args": {"cmd": "ls"}}],
        )
    )
    request = _request_record(MAIN_CONVERSATION, [("call_1", "total 0")])
    first.write(request)

    restarted = TranscriptWriter(transcript)
    restarted.write(dict(request))
    restarted.write(
        _response_record(
            MAIN_CONVERSATION,
            "",
            [{"name": "run_command", "id": "call_1", "args": {"cmd": "ls"}}],
        )
    )

    entries = _entries_by_path(tmp_path)
    calls = [e for e in entries.values() if e["kind"] == "tool_call"]
    results = [e for e in entries.values() if e["kind"] == "tool_result"]
    assert [(e["call_id"], e["path"]) for e in calls] == [
        ("call_1", "tool_calls/call_1.json")
    ]
    assert [(e["call_id"], e["path"]) for e in results] == [
        ("call_1", "tool_results/call_1.txt")
    ]
    assert contract_documents._validate_manifest_links(
        list(entries.values()), "manifest", sealed=True
    ) == []


def test_tool_result_inherits_issuing_conversation(tmp_path: Path) -> None:
    """A result carried by another conversation keeps its call's label.

    Conversation inheritance replays the source's last tool results in the
    receiving Step Execution's first request record; the ``tool_result``
    entry must carry the issuing (source) conversation's ``conversation``
    and ``origin``, not the carrying record's. Re-recording the same
    result under either conversation adds no second artifact. A result
    with no archived call falls back to the carrying record's label.
    """
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    other_conversation = work_conversation("other", 2)
    writer.write(
        _response_record(
            SUB_CONVERSATION,
            "",
            [{"name": "read", "id": "call_inherit", "args": {"path": "x.txt"}}],
        )
    )
    # The receiving conversation's first request carries the source's result.
    writer.write(
        _request_record(other_conversation, [("call_inherit", "body")])
    )

    entries = _entries_by_path(archive_dir)
    result = entries["tool_results/call_inherit.txt"]
    assert result["kind"] == "tool_result"
    assert result["call_id"] == "call_inherit"
    assert result["tool"] == "read"
    assert result["conversation"] == SUB_CONVERSATION
    assert result["origin"] == "subagent"
    # ``at`` is the carrying record's, not the issuing response's.
    transcript_records = [
        json.loads(line)
        for line in (archive_dir / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    carrying_at = next(
        record["at"]
        for record in transcript_records
        if record.get("kind") == "request"
        and record.get("conversation") == other_conversation
        and any(
            isinstance(message, dict)
            and message.get("tool_call_id") == "call_inherit"
            for message in record.get("new_messages", [])
        )
    )
    assert result["at"] == operator_contract.to_z(carrying_at)

    # Re-recording the same result under either conversation is idempotent.
    writer.write(
        _request_record(other_conversation, [("call_inherit", "body")])
    )
    writer.write(
        _request_record(SUB_CONVERSATION, [("call_inherit", "body")])
    )
    again = _entries_by_path(archive_dir)
    results = [e for e in again.values() if e["kind"] == "tool_result"]
    assert [(e["call_id"], e["path"]) for e in results] == [
        ("call_inherit", "tool_results/call_inherit.txt")
    ]
    assert again["tool_results/call_inherit.txt"]["conversation"] == SUB_CONVERSATION
    assert again["tool_results/call_inherit.txt"]["origin"] == "subagent"
    assert contract_documents._validate_manifest_links(
        list(again.values()), "manifest", sealed=True
    ) == []


def test_tool_result_without_call_uses_carrying_label(tmp_path: Path) -> None:
    """A result with no archived call falls back to the carrying label."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    writer.write(_request_record(MAIN_CONVERSATION, [("call_orphan", "out")]))

    entries = _entries_by_path(archive_dir)
    orphan = entries["tool_results/call_orphan.txt"]
    assert orphan["kind"] == "tool_result"
    assert orphan["call_id"] == "call_orphan"
    assert orphan["conversation"] == MAIN_CONVERSATION
    assert orphan["origin"] == "main"
    assert "tool" not in orphan
    assert contract_documents._validate_manifest_links(
        list(entries.values()), "manifest", sealed=False
    ) == []


def test_secrets_redacted_in_artifacts_and_fields(tmp_path: Path) -> None:
    """A registered secret never reaches an artifact or a manifest field."""
    secret = "topsecret-value-987"
    registry = {"GITHUB_TOKEN": secret}
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl", registry)
    writer.write(
        _response_record(
            MAIN_CONVERSATION,
            f"text {secret} here",
            [{"name": "run_command", "id": "call_1", "args": {"cmd": secret}}],
        )
    )
    writer.write(_request_record(MAIN_CONVERSATION, [("call_1", f"out {secret}")]))
    writer.write(_response_record(MAIN_CONVERSATION, f"summary {secret}"))

    marker = "«redacted:GITHUB_TOKEN»"
    assert (archive_dir / "tool_calls/call_1.json").read_text(
        encoding="utf-8"
    ) == f'{{"cmd":"{marker}"}}\n'
    assert (
        archive_dir / "tool_results/call_1.txt"
    ).read_text(encoding="utf-8") == f"out {marker}"
    outputs = sorted(archive_dir.glob("model_outputs/*.txt"))
    assert len(outputs) == 2
    for output in outputs:
        assert secret not in output.read_text(encoding="utf-8")
        assert marker in output.read_text(encoding="utf-8")

    entries = _entries_by_path(archive_dir)
    assert secret not in json.dumps(entries, sort_keys=True)
    assert marker in (archive_dir / "tool_calls/call_1.json").read_text(
        encoding="utf-8"
    )


def test_no_handoff_origin_is_ever_produced(tmp_path: Path) -> None:
    """Origins are only ``main`` or ``subagent``; model outputs stay unpaired."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    for conversation in (MAIN_CONVERSATION, SUB_CONVERSATION, "caller"):
        writer.write(
            _response_record(
                conversation,
                "words",
                [{"name": "read", "id": f"call-{conversation}", "args": {}}],
            )
        )
        writer.write(
            _request_record(conversation, [(f"call-{conversation}", "ok")])
        )

    entries = _entries_by_path(archive_dir)
    _validate_entries(entries)
    origins = {str(entry.get("origin")) for entry in entries.values() if "origin" in entry}
    assert origins <= {"main", "subagent"}
    assert "handoff" not in origins
    for entry in entries.values():
        if entry["kind"] == "model_output":
            assert "call_id" not in entry
            assert "tool" not in entry


def test_call_id_sanitised_for_file_names(tmp_path: Path) -> None:
    """Raw provider call ids never become path segments unsanitised."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    raw = "../../evil/x:y z"
    writer.write(
        _response_record(
            MAIN_CONVERSATION, "", [{"name": "read", "id": raw, "args": {}}]
        )
    )
    writer.write(_request_record(MAIN_CONVERSATION, [(raw, "ok")]))

    entries = _entries_by_path(archive_dir)
    call = next(e for e in entries.values() if e["kind"] == "tool_call")
    result = next(e for e in entries.values() if e["kind"] == "tool_result")
    assert call["call_id"] == result["call_id"] == raw
    assert ".." not in call["path"].split("/")
    assert ".." not in result["path"].split("/")
    assert (archive_dir / call["path"]).is_file()
    assert (archive_dir / result["path"]).is_file()
    assert not (archive_dir / "evil").exists()


def test_tool_index_never_listed_in_manifest(tmp_path: Path) -> None:
    """The metadata index is engine state: present on disk, absent upstream."""
    archive_dir = tmp_path / "archive"
    _write_main_flow(TranscriptWriter(archive_dir / "transcript.jsonl"))

    assert (archive_dir / archive_mod.TOOL_INDEX_FILE_NAME).is_file()
    entries = _entries_by_path(archive_dir)
    assert archive_mod.TOOL_INDEX_FILE_NAME not in entries
    assert not any(
        entry["kind"] in ("tool_call", "tool_result", "model_output")
        and entry["path"].endswith(archive_mod.TOOL_INDEX_FILE_NAME)
        for entry in entries.values()
    )


def test_atomic_write_residue_never_classifies_as_evidence(tmp_path: Path) -> None:
    """Crash-orphaned `<final>.tmp.<rand>` files stay out of the manifest."""
    archive_dir = tmp_path / "archive"
    _write_main_flow(TranscriptWriter(archive_dir / "transcript.jsonl"))
    residue = archive_dir / "tool_calls" / "call_1.json.tmp.Ab12Cd34"
    residue.write_bytes(b'{"cmd":"ls"}\n')
    # A dotted call id keeps its finished artifact listed.
    dotted = archive_dir / "tool_calls" / "a.tmp.b.json"
    dotted.write_bytes(b"{}\n")

    entries = _entries_by_path(archive_dir)
    assert "tool_calls/call_1.json.tmp.Ab12Cd34" not in entries
    assert "tool_calls/a.tmp.b.json" in entries
    assert entries["tool_calls/a.tmp.b.json"]["kind"] == "tool_call"


def test_legacy_kinds_keep_their_1_1_shape(tmp_path: Path) -> None:
    """Entries of the 1.1 kinds carry exactly the 1.1 fields."""
    archive_dir = tmp_path / "archive"
    (archive_dir / "validation" / "targeted").mkdir(parents=True)
    (archive_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (archive_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (archive_dir / "validation" / "targeted" / "junit.xml").write_text(
        "<ok/>\n", encoding="utf-8"
    )
    (archive_dir / "notes.md").write_text("hi\n", encoding="utf-8")
    (archive_dir / "blob.bin").write_bytes(b"\x00\x01")

    entries = _entries_by_path(archive_dir)
    assert set(entries) == {
        "events.jsonl",
        "transcript.jsonl",
        "validation/targeted/junit.xml",
        "notes.md",
        "blob.bin",
    }
    for entry in entries.values():
        assert set(entry) == {"path", "kind", "media_type", "size", "sha256"}
    assert entries["events.jsonl"]["kind"] == "events"
    assert entries["transcript.jsonl"]["kind"] == "transcript"
    assert entries["validation/targeted/junit.xml"]["kind"] == "command_output"
    assert entries["notes.md"]["kind"] == "record"
    assert entries["blob.bin"]["kind"] == "other"
    assert entries["blob.bin"]["media_type"] == "application/octet-stream"


def test_transcript_bytes_unchanged_by_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Derivation adds no transcript line, field or byte."""
    plain_dir = tmp_path / "plain"
    live_dir = tmp_path / "live"
    monkeypatch.setattr(
        transcript_module, "derive_tool_artifacts", lambda *args: None
    )
    plain_writer = TranscriptWriter(plain_dir / "transcript.jsonl")
    _write_main_flow(plain_writer)

    monkeypatch.undo()
    live_writer = TranscriptWriter(live_dir / "transcript.jsonl")
    _write_main_flow(live_writer)

    plain_lines = (plain_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    live_lines = (live_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(plain_lines) == len(live_lines) == 3
    for plain_line, live_line in zip(plain_lines, live_lines):
        plain_record = json.loads(plain_line)
        live_record = json.loads(live_line)
        # Only the writer-stamped ``seq``/``at`` may differ between runs.
        assert {k: v for k, v in plain_record.items() if k not in ("seq", "at")} == {
            k: v for k, v in live_record.items() if k not in ("seq", "at")
        }


def test_concurrent_subagent_writers_stay_consistent(tmp_path: Path) -> None:
    """Sub-agent threads share the per-file discipline without torn metadata."""
    archive_dir = tmp_path / "archive"
    writer = TranscriptWriter(archive_dir / "transcript.jsonl")
    errors: list[str] = []

    def _run(index: int) -> None:
        try:
            conversation = subagent_conversation(f"call-worker-{index}")
            call_id = f"worker-{index}-call"
            writer.write(
                _response_record(
                    conversation,
                    "",
                    [{"name": "read", "id": call_id, "args": {"n": index}}],
                )
            )
            writer.write(_request_record(conversation, [(call_id, f"out-{index}")]))
        except Exception as exc:  # pragma: no cover - test failure surface
            errors.append(str(exc))

    threads = [threading.Thread(target=_run, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    entries = _entries_by_path(archive_dir)
    _validate_entries(entries)
    calls = [e for e in entries.values() if e["kind"] == "tool_call"]
    results = [e for e in entries.values() if e["kind"] == "tool_result"]
    assert len(calls) == len(results) == 8
    assert {e["call_id"] for e in calls} == {e["call_id"] for e in results}
    assert all(e["origin"] == "subagent" for e in calls + results)
    lines = (archive_dir / archive_mod.TOOL_INDEX_FILE_NAME).read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 16
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_producer_advertises_contract_1_2() -> None:
    """Every advertised version moved to contract minor 1.2."""
    assert operator_contract.CONTRACT_MAJOR == 1
    assert operator_contract.CONTRACT_MINOR == 2
    assert operator_contract.CONTRACT_VERSION == "1.2"
    assert agentctl.CONTRACT_VERSION == "1.2"
    assert CONTRACT_VERSION == "1.2"
    assert CONTRACT_PIN == "v1.2.0"
    kinds: tuple[str, ...] = ("tool_call", "tool_result", "model_output")
    for kind in kinds:
        assert kind in get_args(archive_mod.ManifestKind)


def test_show_attempt_lists_artifacts_live_and_sealed(tmp_path: Path) -> None:
    """``attempts show`` builds the unsealed and Sealed manifests with fields."""
    from coding_agent.ledger.store import RunLedger
    from coding_agent.operator.store import OperatorStore

    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    try:
        store.write_instance_metadata(target_repository="example-owner/example-repo")
        ledger.open_attempt(
            "#7/1", effective_configuration={"repo": "example-owner/example-repo", "issue": 7}
        )
        archive_dir = archive_mod.archive_dir_for(
            tmp_path, "example-owner/example-repo", "#7/1"
        )
        writer = TranscriptWriter(archive_dir / "transcript.jsonl")
        _write_main_flow(writer)
        writer.write(
            _response_record(
                MAIN_CONVERSATION,
                "",
                [{"name": "run_command", "id": "call_last", "args": {"cmd": "exit"}}],
            )
        )

        unsealed = attempt_archives.show_attempt(
            ledger_path=ledger_path, state_dir=tmp_path, attempt_id="#7/1"
        )
        assert unsealed is not None
        assert unsealed["attempt"]["sealed"] is False
        assert contract_documents.validate_attempts_show(unsealed) == []
        by_path = {e["path"]: e for e in unsealed["manifest"]}
        assert by_path["tool_calls/call_1.json"]["tool"] == "run_command"
        assert by_path["tool_results/call_1.txt"]["call_id"] == "call_1"

        ledger.record_terminal_outcome("#7/1", outcome="delivered", reason="done")
        ledger.set_attempt_state("#7/1", "finished")
        sealed = attempt_archives.show_attempt(
            ledger_path=ledger_path, state_dir=tmp_path, attempt_id="#7/1"
        )
        assert sealed is not None
        assert sealed["attempt"]["sealed"] is True
        assert contract_documents.validate_attempts_show(sealed) == []
        sealed_by_path = {e["path"]: e for e in sealed["manifest"]}
        assert "tool_calls/call_last.json" in sealed_by_path
        assert not any(
            e.get("call_id") == "call_last" and e.get("kind") == "tool_result"
            for e in sealed["manifest"]
        )
        assert contract_documents.validate_manifest_entry(
            sealed_by_path["tool_calls/call_last.json"]
        ) == []
        document = attempt_archives.export_manifest_document(
            attempt=sealed["attempt"], files=sealed["manifest"]
        )
        assert document["contract"] == "1.2"
        assert contract_documents.validate_export_manifest(document) == []
    finally:
        store.close()
        ledger.close()
