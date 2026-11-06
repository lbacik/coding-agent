"""Attempt Archives and Attempt-scoped Contract Events (issue #255).

Contract: adoption step 4 of the operator contract (#245). An Attempt Archive
is the Attempt's per-Attempt directory on the state volume
(``evidence/<owner>/<repo>/<issue>/<n>``), created at the Claim boundary.
The Contract Event producer also writes each Attempt-scoped event to that
Attempt's ``events.jsonl``. ``attempts list`` pages AttemptSummaries newest
first; ``attempts show`` returns one AttemptSummary plus a manifest of
``{path, kind, media_type, size, sha256}`` entries computed on read. Archive
reads happen directly in the ``agentctl`` process over the volume, through
the one code path Offline Read reuses.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl
from coding_agent.ledger.store import RunLedger
from coding_agent.operator import attempts as attempt_archives
from coding_agent.operator.paths import STATE_DIR_ENV_VAR
from coding_agent.operator.store import OperatorStore
from coding_agent.runtime.redaction import SecretRegistry

REPO = "example-owner/example-repo"


def _effective_config() -> dict[str, Any]:
    return {"repo": REPO, "issue": 7}


def _ledger_with_instance(
    tmp_path: Path, *, secrets: dict[str, str] | None = None
) -> tuple[RunLedger, OperatorStore]:
    """A ledger on ``tmp_path`` with this start's Target Repository recorded."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    registry = SecretRegistry(secrets) if secrets is not None else None
    ledger = RunLedger(ledger_path, registry=registry)
    store = OperatorStore(ledger_path)
    store.write_instance_metadata(target_repository=REPO)
    return ledger, store


def _archive(tmp_path: Path, attempt_id: str) -> Path:
    return attempt_archives.archive_dir_for(tmp_path, REPO, attempt_id)


def _read_lines(archive_dir: Path) -> list[dict[str, Any]]:
    lines = (archive_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _run_call(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    code = agentctl.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    return code, json.loads(captured.out)


def test_claim_opens_archive_with_claim_opened_event(tmp_path: Path) -> None:
    """The Claim boundary creates the archive with its first Contract Event."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        assert archive.is_dir()
        records = _read_lines(archive)
        assert len(records) == 1
        assert records[0]["type"] == "claim_opened"
        assert records[0]["attempt_id"] == "#7/1"
        assert "claim opened #7/1" in records[0]["text"]
    finally:
        store.close()
        ledger.close()


def test_list_newest_first_and_paging(tmp_path: Path) -> None:
    """After two Attempts, ``list`` pages newest first through ``--before``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.open_attempt("#7/2", effective_configuration=_effective_config())
        ledger_path = tmp_path / "run-ledger.sqlite3"

        page, next_before = attempt_archives.list_attempts_page(ledger_path, limit=20)
        assert [entry["attempt_id"] for entry in page] == ["#7/2", "#7/1"]
        assert next_before is None

        first, cursor = attempt_archives.list_attempts_page(ledger_path, limit=1)
        assert [entry["attempt_id"] for entry in first] == ["#7/2"]
        assert cursor is not None

        second, tail = attempt_archives.list_attempts_page(
            ledger_path, limit=1, before=cursor
        )
        assert [entry["attempt_id"] for entry in second] == ["#7/1"]
        assert tail is None

        with pytest.raises(ValueError):
            attempt_archives.list_attempts_page(ledger_path, limit=101)
        with pytest.raises(ValueError):
            attempt_archives.list_attempts_page(ledger_path, limit=0)
        with pytest.raises(attempt_archives.InvalidCursor):
            attempt_archives.list_attempts_page(ledger_path, before="not-a-cursor")
    finally:
        store.close()
        ledger.close()


def test_active_attempt_listed_while_running(tmp_path: Path) -> None:
    """The active Attempt is listed while it runs, with an empty outcome."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        page, _ = attempt_archives.list_attempts_page(
            tmp_path / "run-ledger.sqlite3"
        )
        assert len(page) == 1
        summary = page[0]
        assert summary["attempt_id"] == "#7/1"
        assert summary["issue_number"] == 7
        assert summary["attempt_number"] == 1
        assert summary["state"] == "open"
        assert summary["outcome"] is None
    finally:
        store.close()
        ledger.close()


def test_show_manifest_kinds_sizes_digests(tmp_path: Path) -> None:
    """``show`` returns the summary and a manifest computed on read."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        payloads = {
            "transcript.jsonl": '{"role": "assistant"}\n',
            "validation/targeted/junit.xml": "<ok/>\n",
            "tool-results/snapshot.json": '{"digest": "abc"}\n',
            "outcome.md": "# done\n",
            "notes.bin": "\x00\x01\x02",
        }
        for relative, text in payloads.items():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        assert shown["attempt"]["attempt_id"] == "#7/1"
        by_path = {entry["path"]: entry for entry in shown["manifest"]}
        assert by_path["transcript.jsonl"]["kind"] == "transcript"
        assert by_path["validation/targeted/junit.xml"]["kind"] == "command_output"
        assert by_path["tool-results/snapshot.json"]["kind"] == "record"
        assert by_path["outcome.md"]["kind"] == "record"
        assert by_path["notes.bin"]["kind"] == "other"
        assert by_path["events.jsonl"]["kind"] == "record"
        assert by_path["validation/targeted/junit.xml"]["media_type"] == (
            "application/xml"
        )
        assert by_path["notes.bin"]["media_type"] == "application/octet-stream"
        for relative, text in payloads.items():
            entry = by_path[relative]
            raw = text.encode("utf-8")
            assert entry["size"] == len(raw)
            assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert [entry["path"] for entry in shown["manifest"]] == sorted(by_path)
    finally:
        store.close()
        ledger.close()


def test_unknown_attempt_is_not_found(tmp_path: Path) -> None:
    """An unknown Attempt answers ``not_found``: ``show`` returns ``None``."""
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        assert (
            attempt_archives.show_attempt(
                ledger_path=tmp_path / "run-ledger.sqlite3",
                state_dir=tmp_path,
                attempt_id="#7/9",
            )
            is None
        )
        assert (
            attempt_archives.show_attempt(
                ledger_path=tmp_path / "missing.sqlite3",
                state_dir=tmp_path,
                attempt_id="#7/1",
            )
            is None
        )
    finally:
        store.close()
        ledger.close()


def test_manifest_excludes_private_files(tmp_path: Path) -> None:
    """The manifest never lists the ledger, checkpoints, workspaces, mirrors,
    caches, credentials or the Workflow Configuration — even when a copy
    sits inside the archive.
    """
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        private = [
            "run-ledger.sqlite3",
            "run-ledger.sqlite3-wal",
            "run-ledger.sqlite3-shm",
            "checkpoint.json",
            "checkpoint/state.json",
            "checkpoints/state.json",
            "workspace/tree.txt",
            "workspaces/tree.txt",
            "mirror/pack",
            "mirrors/pack",
            "cache/blob",
            "caches/blob",
            ".cache/blob",
            "__pycache__/module.pyc",
            "baseline-base/tree.txt",
            ".env",
            ".git-credentials",
            "deploy-token.txt",
            "github_secret.yaml",
            "signing.credential",
            "deploy.pem",
            "deploy.key",
            "workflow.yml",
            "workflow.yaml",
            "workflow-config.yml",
            "nested/ws/data.txt",
        ]
        for relative in private:
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("private", encoding="utf-8")
        nested_git = archive / "nested" / "ws" / ".git"
        nested_git.mkdir(parents=True, exist_ok=True)
        (nested_git / "HEAD").write_text("ref", encoding="utf-8")
        shown = attempt_archives.show_attempt(
            ledger_path=tmp_path / "run-ledger.sqlite3",
            state_dir=tmp_path,
            attempt_id="#7/1",
        )
        assert shown is not None
        paths = [entry["path"] for entry in shown["manifest"]]
        assert "events.jsonl" in paths
        for relative in private:
            assert relative not in paths
        assert not any(path.startswith("nested/") for path in paths)
    finally:
        store.close()
        ledger.close()


def test_events_jsonl_redacted_and_capped(tmp_path: Path) -> None:
    """Every ``events.jsonl`` line is a redacted Contract Event, text ≤ 300."""
    secret = "token-secret-value"
    ledger, store = _ledger_with_instance(tmp_path, secrets={"GITHUB_TOKEN": secret})
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.emit_attempt_event("#7/1", "native.handoff", f"leaks {secret} " + "z" * 500)
        ledger.record_terminal_outcome(
            "#7/1",
            outcome="delivered",
            reason=f"done {secret} " + "y" * 500,
        )
        ledger.set_attempt_state("#7/1", "finished")
        archive = _archive(tmp_path, "#7/1")
        raw = (archive / "events.jsonl").read_text(encoding="utf-8")
        assert secret not in raw
        records = _read_lines(archive)
        assert [record["type"] for record in records] == [
            "claim_opened",
            "native.handoff",
            "outcome_recorded",
            "attempt_finished",
            "sealed",
        ]
        for record in records:
            assert record["attempt_id"] == "#7/1"
            assert len(record["text"]) <= 300
            assert secret not in record["text"]
        # The file stores ASCII-escaped JSON, so the marker's ASCII-safe
        # core is asserted on the raw bytes and the literal marker on the
        # decoded texts that carried the secret.
        assert "redacted:GITHUB_TOKEN" in raw
        leaking = [
            record
            for record in records
            if record["type"] in ("outcome_recorded", "native.handoff")
        ]
        assert len(leaking) == 2
        assert all("«redacted:GITHUB_TOKEN»" in record["text"] for record in leaking)
    finally:
        store.close()
        ledger.close()


def test_agentctl_list_pages_and_rejects_bad_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentctl attempts list`` pages newest first; ``--limit 101`` fails."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        ledger.open_attempt("#7/2", effective_configuration=_effective_config())

        code, envelope = _run_call(["--contract", "1", "attempts", "list"], capsys)
        assert code == 0
        assert envelope["ok"] is True
        result = envelope["result"]
        assert [entry["attempt_id"] for entry in result["attempts"]] == [
            "#7/2",
            "#7/1",
        ]
        assert result["next_before"] is None

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "list", "--limit", "1"], capsys
        )
        assert code == 0
        first = envelope["result"]
        assert [entry["attempt_id"] for entry in first["attempts"]] == ["#7/2"]
        assert first["next_before"]

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "list", "--limit", "1",
             "--before", str(first["next_before"])],
            capsys,
        )
        assert code == 0
        assert [entry["attempt_id"] for entry in envelope["result"]["attempts"]] == [
            "#7/1"
        ]
        assert envelope["result"]["next_before"] is None

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "list", "--limit", "101"], capsys
        )
        assert code == 1
        assert envelope["error"]["code"] == "invalid_argument"

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "list", "--before", "bogus"], capsys
        )
        assert code == 1
        assert envelope["error"]["code"] == "invalid_argument"
    finally:
        store.close()
        ledger.close()


def test_agentctl_show_and_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agentctl attempts show`` returns the manifest; unknown is ``not_found``."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))
    ledger, store = _ledger_with_instance(tmp_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        archive = _archive(tmp_path, "#7/1")
        (archive / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "show", "#7/1"], capsys
        )
        assert code == 0
        assert envelope["ok"] is True
        assert envelope["result"]["attempt"]["attempt_id"] == "#7/1"
        paths = [entry["path"] for entry in envelope["result"]["manifest"]]
        assert "events.jsonl" in paths
        assert "transcript.jsonl" in paths

        code, envelope = _run_call(
            ["--contract", "1", "attempts", "show", "#7/9"], capsys
        )
        assert code == 5
        assert envelope["error"]["code"] == "not_found"
        assert envelope["error"]["retryable"] is False
    finally:
        store.close()
        ledger.close()


def test_agentctl_list_empty_without_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a ledger, ``list`` is an empty page and ``show`` is ``not_found``."""
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path / "fresh-volume"))
    code, envelope = _run_call(["--contract", "1", "attempts", "list"], capsys)
    assert code == 0
    assert envelope["result"] == {"attempts": [], "next_before": None}
    code, envelope = _run_call(
        ["--contract", "1", "attempts", "show", "#7/1"], capsys
    )
    assert code == 5
    assert envelope["error"]["code"] == "not_found"


def test_ledger_without_instance_row_stays_db_only(tmp_path: Path) -> None:
    """A ledger without its Target Repository records the DB row alone."""
    ledger_path = tmp_path / "run-ledger.sqlite3"
    ledger = RunLedger(ledger_path)
    try:
        ledger.open_attempt("#7/1", effective_configuration=_effective_config())
        assert not (tmp_path / "evidence").exists()
        connection = sqlite3.connect(str(ledger_path))
        try:
            rows = connection.execute(
                "SELECT type FROM operator_events"
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == ["claim_opened"]
    finally:
        ledger.close()
