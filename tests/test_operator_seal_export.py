"""Sealing and export (adoption step 4, #256).

A Consumer fetches an Attempt's evidence and knows when it is final.
Sealing happens once the Attempt is finished/released and every recorded
effect has its receipt; ``attempts show`` reports ``sealed`` and
``attempts export`` streams a pax tar with ``manifest.json`` first.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator.paths import STATE_DIR_ENV_VAR
from coding_agent.operator.store import OperatorStore

REPO = "example-owner/example-repo"


def _effective_config() -> dict[str, Any]:
    return {"repo": REPO, "issue": 7}


def _ledger_with_instance(tmp_path: Path) -> tuple[RunLedger, OperatorStore]:
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(target_repository=REPO)
    return ledger, store


def _archive(tmp_path: Path, attempt_id: str) -> Path:
    return attempt_archives.archive_dir_for(tmp_path, REPO, attempt_id)


def _read_lines(archive_dir: Path) -> list[dict[str, Any]]:
    lines = (archive_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_finished_attempt_seals_with_event(tmp_path: Path) -> None:
    """After delivery, show reports sealed with sealed_at after finished."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="delivered", reason="done")
        shown_mid = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown_mid is not None
        assert shown_mid["attempt"]["sealed"] is False
        assert shown_mid["attempt"]["sealed_at"] is None
        assert not (archive / "manifest.json").is_file()
        ledger.set_attempt_state("#7/1", "finished")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is True
        sealed_at = shown["attempt"]["sealed_at"]
        assert isinstance(sealed_at, str) and sealed_at
        records = _read_lines(archive)
        types = [record["type"] for record in records]
        assert types[-2:] == ["attempt_finished", "sealed"]
        assert types.index("attempt_finished") < types.index("sealed")
        sealed_doc = attempt_archives.read_sealed_manifest(archive)
        assert sealed_doc is not None
        assert sealed_doc["sealed_at"] == sealed_at
        assert sealed_doc["attempt_id"] == "#7/1"
        by_path = {entry["path"]: entry for entry in shown["manifest"]}
        assert by_path["transcript.jsonl"]["sha256"] == hashlib.sha256(
            b"{}\n"
        ).hexdigest()
    finally:
        store.close()
        ledger.close()


def test_released_attempt_seals(tmp_path: Path) -> None:
    """A released Attempt seals too."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.set_attempt_state("#7/1", "released")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is True
        assert isinstance(shown["attempt"]["sealed_at"], str)
        archive = _archive(tmp_path, "#7/1")
        records = _read_lines(archive)
        types = [record["type"] for record in records]
        assert types[-2:] == ["attempt_released", "sealed"]
    finally:
        store.close()
        ledger.close()


def test_effect_intent_defers_seal_until_receipt(tmp_path: Path) -> None:
    """Finishing with an unreceipted effect stays unsealed until backfill."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.record_effect_intent("#7/1", "publish", payload={"branch": "b"})
        ledger.record_terminal_outcome("#7/1", outcome="delivered", reason="done")
        ledger.set_attempt_state("#7/1", "finished")
        archive = _archive(tmp_path, "#7/1")
        assert not (archive / "manifest.json").is_file()
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is False
        ledger.record_effect_receipt("#7/1", "publish", remote_id="123")
        assert (archive / "manifest.json").is_file()
        shown2 = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown2 is not None
        assert shown2["attempt"]["sealed"] is True
        records = _read_lines(archive)
        assert [r["type"] for r in records][-1] == "sealed"
    finally:
        store.close()
        ledger.close()


def _export_tar_bytes(
    archive: Path | None,
    document: dict[str, Any],
    entries: list[dict[str, Any]],
) -> bytes:
    buffer = io.BytesIO()
    attempt_archives.write_export_tar(
        archive_dir=archive, document=document, entries=entries, fileobj=buffer
    )
    return buffer.getvalue()


def test_export_sealed_tar_manifest_first(tmp_path: Path) -> None:
    """Export streams pax tar with manifest.json first matching sealed digests."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        payloads = {
            "transcript.jsonl": '{"role": "x"}\n',
            "validation/targeted/junit.xml": "<ok/>\n",
        }
        for relative, text in payloads.items():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="delivered", reason="done")
        ledger.set_attempt_state("#7/1", "finished")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is True
        entries = list(shown["manifest"])
        document = attempt_archives.export_manifest_document(
            attempt_id="#7/1",
            sealed=True,
            sealed_at=str(shown["attempt"]["sealed_at"]),
            entries=entries,
        )
        raw = _export_tar_bytes(archive, document, entries)
        reader = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
        try:
            members = reader.getmembers()
            assert members[0].name == "manifest.json"
            assert raw[:2] != b"\x1f\x8b"
            manifest_raw = reader.extractfile(members[0])
            assert manifest_raw is not None
            manifest_doc = json.loads(manifest_raw.read().decode("utf-8"))
            assert manifest_doc["sealed"] is True
            manifest_by_path = {
                entry["path"]: entry for entry in manifest_doc["entries"]
            }
            for relative, text in payloads.items():
                member = reader.getmember(relative)
                data = reader.extractfile(member)
                assert data is not None
                content = data.read()
                assert content == text.encode("utf-8")
                assert manifest_by_path[relative]["sha256"] == hashlib.sha256(
                    content
                ).hexdigest()
        finally:
            reader.close()
    finally:
        store.close()
        ledger.close()


def test_export_path_filters_and_unknown_is_not_found(tmp_path: Path) -> None:
    """--path exports only that member; unknown path is not_found."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "a.txt").write_text("a", encoding="utf-8")
        (archive / "b.txt").write_text("b", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/1", "finished")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        filtered = attempt_archives.filter_manifest_entries(
            list(shown["manifest"]), ["a.txt"]
        )
        assert filtered is not None
        assert [e["path"] for e in filtered] == ["a.txt"]
        document = attempt_archives.export_manifest_document(
            attempt_id="#7/1",
            sealed=True,
            sealed_at=str(shown["attempt"]["sealed_at"]),
            entries=filtered,
        )
        raw = _export_tar_bytes(archive, document, filtered)
        reader = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
        try:
            names = reader.getnames()
            assert names == ["manifest.json", "a.txt"]
        finally:
            reader.close()
        assert (
            attempt_archives.filter_manifest_entries(
                list(shown["manifest"]), ["missing.txt"]
            )
            is None
        )
        assert (
            attempt_archives.filter_manifest_entries(
                list(shown["manifest"]), ["../escape"]
            )
            is None
        )
    finally:
        store.close()
        ledger.close()


def test_unsealed_export_stays_readable(tmp_path: Path) -> None:
    """An unsealed archive can be exported and stays readable."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "notes.txt").write_text("draft", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is False
        assert not (archive / "manifest.json").exists()
        entries = list(shown["manifest"])
        document = attempt_archives.export_manifest_document(
            attempt_id="#7/1", sealed=False, sealed_at=None, entries=entries
        )
        raw = _export_tar_bytes(archive, document, entries)
        reader = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
        try:
            names = reader.getnames()
            assert names[0] == "manifest.json"
            assert "notes.txt" in names
            manifest_raw = reader.extractfile(reader.getmember("manifest.json"))
            assert manifest_raw is not None
            manifest_doc = json.loads(manifest_raw.read().decode("utf-8"))
            assert manifest_doc["sealed"] is False
        finally:
            reader.close()
        assert not (archive / "manifest.json").exists()
        shown2 = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown2 is not None
        assert shown2["attempt"]["sealed"] is False
    finally:
        store.close()
        ledger.close()


def test_agentctl_export_unknown_attempt_is_not_found(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """Unknown Attempt answers not_found with no tar bytes on stdout."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        code = agentctl.main(["--contract", "1", "attempts", "export", "#7/9"])
        captured = capsys.readouterr()
        assert code == 5
        assert captured.err == ""
        envelope = json.loads(captured.out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "not_found"
    finally:
        store.close()
        ledger.close()


def test_agentctl_export_unknown_path_is_not_found(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """Unknown path answers not_found with no tar bytes on stdout."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "a.txt").write_text("a", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/1", "finished")
        code = agentctl.main(
            ["--contract", "1", "attempts", "export", "#7/1", "--path", "missing.txt"]
        )
        captured = capsys.readouterr()
        assert code == 5
        assert captured.err == ""
        envelope = json.loads(captured.out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "not_found"
        assert "manifest.json" not in captured.out
    finally:
        store.close()
        ledger.close()


def test_agentctl_export_streams_tar_first_member_manifest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """agentctl export streams tar bytes with manifest.json first."""
    import sys

    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "a.txt").write_text("hello", encoding="utf-8")
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/1", "finished")
        buffer = io.BytesIO()

        class _Stdout:
            def __init__(self) -> None:
                self.buffer = buffer

            def write(self, text: str) -> int:
                raise AssertionError("export must not emit JSON text")

            def flush(self) -> None:
                return None

        monkeypatch.setattr(sys, "stdout", _Stdout())
        code = agentctl.main(["--contract", "1", "attempts", "export", "#7/1"])
        assert code == 0
        raw = buffer.getvalue()
        assert len(raw) > 0
        reader = tarfile.open(fileobj=io.BytesIO(raw), mode="r")
        try:
            assert reader.getnames()[0] == "manifest.json"
        finally:
            reader.close()
    finally:
        store.close()
        ledger.close()
        connection = sqlite3.connect(str(tmp_path / "run-ledger.sqlite3"))
        try:
            connection.close()
        except sqlite3.Error:
            pass


def test_corrupt_manifest_heals_on_seal(tmp_path: Path) -> None:
    """A torn seal write reads unsealed but does not block re-sealing."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "notes.txt").write_text("draft", encoding="utf-8")
        (archive / "manifest.json").write_text("torn{{{", encoding="utf-8")
        shown_before = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown_before is not None
        assert shown_before["attempt"]["sealed"] is False
        ledger.record_terminal_outcome("#7/1", outcome="failed", reason="x")
        ledger.set_attempt_state("#7/1", "finished")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is True
        assert isinstance(shown["attempt"]["sealed_at"], str)
        assert attempt_archives.read_sealed_manifest(archive) is not None
    finally:
        store.close()
        ledger.close()


def test_receipt_reentry_retries_failed_seal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A receipt that changes nothing still retries a failed best-effort seal."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.record_effect_intent("#7/1", "publish", payload={"branch": "b"})
        ledger.record_effect_receipt("#7/1", "publish", remote_id="123")
        ledger.record_terminal_outcome("#7/1", outcome="delivered", reason="done")
        real_write = attempt_archives.write_sealed_manifest_atomically

        def _fail_once(archive_dir: Path, document: dict[str, Any]) -> None:
            monkeypatch.setattr(
                attempt_archives,
                "write_sealed_manifest_atomically",
                real_write,
            )
            raise OSError("disk full")

        monkeypatch.setattr(
            attempt_archives, "write_sealed_manifest_atomically", _fail_once
        )
        ledger.set_attempt_state("#7/1", "finished")
        archive = _archive(tmp_path, "#7/1")
        assert not (archive / "manifest.json").is_file()
        ledger.record_effect_receipt("#7/1", "publish", remote_id="123")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["sealed"] is True
        sealed_events = [
            record
            for record in _read_lines(archive)
            if record["type"] == "sealed"
        ]
        assert len(sealed_events) == 1
    finally:
        store.close()
        ledger.close()
