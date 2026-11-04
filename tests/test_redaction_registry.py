"""The redaction registry choke point (contract §18; L3-ERR-11, L3-ERR-12).

Every registered secret is replaced by ``«redacted:<name>»`` in both log
streams, in the transcript, in tool arguments and in tool results, and in
the ledger; no remote URL carries the token; child processes started for
commands run on the model's behalf see none of the registered secrets in
their environment; exception messages and tracebacks pass through the same
registry.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from coding_agent.runtime.redaction import (
    RedactingWriter,
    SecretRegistry,
    format_exception_redacted,
    format_traceback_redacted,
    install_stream_redaction,
    redact_text,
    registry_from,
    temporary_git_credential_helper,
)


def _registry() -> tuple[SecretRegistry, dict[str, str]]:
    secrets = {
        "GITHUB_TOKEN": "github_pat_testtoken1234567890",
        "WORKER_ANTHROPIC_KEY": "sk-ant-testkey-abcdef",
        "OPENAI_API_KEY": "sk-testkey-xyz",
    }
    return SecretRegistry(secrets), secrets


def _assert_no_secrets(text: str, secrets: dict[str, str]) -> None:
    """Every registered value is absent from an output surface (§18)."""
    for value in secrets.values():
        assert value not in text


def _assert_markers_present(text: str, names: Iterable[str]) -> None:
    """Every registered name's marker replaces its value (§18, L3-ERR-11)."""
    for name in names:
        assert f"«redacted:{name}»" in text


def test_every_registered_secret_is_replaced_by_its_named_marker() -> None:
    registry, secrets = _registry()
    text = (
        f"token {secrets['GITHUB_TOKEN']} key {secrets['WORKER_ANTHROPIC_KEY']} "
        f"other {secrets['OPENAI_API_KEY']}"
    )
    redacted = registry.redact(text)
    for value in secrets.values():
        assert value not in redacted
    assert redacted == (
        "token «redacted:GITHUB_TOKEN» key «redacted:WORKER_ANTHROPIC_KEY» "
        "other «redacted:OPENAI_API_KEY»"
    )


def test_json_escaped_secrets_are_redacted_in_serialized_lines() -> None:
    secret = 'tok"en\nwith-quotes'
    registry = SecretRegistry({"QUOTED": secret})
    import json

    line = json.dumps({"output": f"saw {secret} here"}, ensure_ascii=False)
    assert secret not in line  # it is present only in escaped form
    redacted = registry.redact(line)
    assert "«redacted:QUOTED»" in redacted
    assert secret.replace('"', '\\"') not in redacted


def test_empty_values_never_enter_the_registry() -> None:
    registry = SecretRegistry({"EMPTY": "", "SET": "value"})
    assert registry.names == ("SET",)
    assert registry.redact("anything at all") == "anything at all"


def test_overlapping_secrets_redact_longest_first() -> None:
    registry = SecretRegistry({"SHORT": "abc", "LONG": "abcdef"})
    assert registry.redact("abcdef") == "«redacted:LONG»"


def test_redact_json_replaces_secrets_in_keys_values_and_nesting() -> None:
    registry, secrets = _registry()
    payload = {
        f"key-{secrets['GITHUB_TOKEN']}": [
            secrets["WORKER_ANTHROPIC_KEY"],
            {"nested": secrets["OPENAI_API_KEY"]},
            42,
            None,
        ]
    }
    redacted = registry.redact_json(payload)
    assert isinstance(redacted, dict)
    rendered = str(redacted)
    for value in secrets.values():
        assert value not in rendered
    assert "«redacted:GITHUB_TOKEN»" in rendered
    assert "«redacted:WORKER_ANTHROPIC_KEY»" in rendered
    assert "«redacted:OPENAI_API_KEY»" in rendered


def test_registry_from_accepts_every_redact_form() -> None:
    mapping = {"GITHUB_TOKEN": "token-value"}
    assert registry_from(None).names == ()
    assert registry_from("token-value").redact("token-value") == "«redacted:credential»"
    assert registry_from(mapping).redact("token-value") == "«redacted:GITHUB_TOKEN»"
    direct = SecretRegistry(mapping)
    assert registry_from(direct) is direct


def test_redact_text_is_the_single_string_choke_point() -> None:
    assert redact_text("s3cr3t here", "s3cr3t") == "«redacted:credential» here"
    assert redact_text("plain", None) == "plain"


def test_both_log_streams_filter_every_registered_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry, secrets = _registry()
    uninstall = install_stream_redaction(registry)
    try:
        print(f"stdout carries {secrets['GITHUB_TOKEN']}")
        print(f"stderr carries {secrets['WORKER_ANTHROPIC_KEY']}", file=sys.stderr)
    finally:
        uninstall()
    captured = capsys.readouterr()
    for value in secrets.values():
        assert value not in captured.out
        assert value not in captured.err
    assert "«redacted:GITHUB_TOKEN»" in captured.out
    assert "«redacted:WORKER_ANTHROPIC_KEY»" in captured.err


def test_stream_redaction_uninstall_restores_both_streams(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry, secrets = _registry()
    uninstall = install_stream_redaction(registry)
    uninstall()
    print(f"after uninstall {secrets['GITHUB_TOKEN']}")
    assert secrets["GITHUB_TOKEN"] in capsys.readouterr().out


def test_exception_messages_and_tracebacks_are_redacted() -> None:
    registry, secrets = _registry()
    try:
        raise RuntimeError(f"request failed with body {secrets['GITHUB_TOKEN']}")
    except RuntimeError as exc:
        message = format_exception_redacted(exc, registry)
        rendered_traceback = format_traceback_redacted(exc, registry)
    assert secrets["GITHUB_TOKEN"] not in message
    assert "«redacted:GITHUB_TOKEN»" in message
    assert secrets["GITHUB_TOKEN"] not in rendered_traceback
    assert "«redacted:GITHUB_TOKEN»" in rendered_traceback


def test_redacting_writer_delegates_flush_and_encoding(tmp_path: Path) -> None:
    registry, secrets = _registry()
    target = tmp_path / "stream.log"
    with target.open("w", encoding="utf-8") as handle:
        writer = RedactingWriter(handle, registry)
        assert writer.encoding == "utf-8"
        writer.write(f"token {secrets['GITHUB_TOKEN']}\n")
        writer.flush()
    assert secrets["GITHUB_TOKEN"] not in target.read_text(encoding="utf-8")
    assert "«redacted:GITHUB_TOKEN»" in target.read_text(encoding="utf-8")


def test_child_environment_carries_no_registered_secret() -> None:
    registry, secrets = _registry()
    env = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": secrets["GITHUB_TOKEN"],
        "WORKER_ANTHROPIC_KEY": secrets["WORKER_ANTHROPIC_KEY"],
        "OPENAI_API_KEY": secrets["OPENAI_API_KEY"],
        "LEAKED_ELSEWHERE": f"prefix-{secrets['GITHUB_TOKEN']}-suffix",
        "UNRELATED": "nothing-secret-here",
    }
    cleaned = registry.scrub_env(env)
    assert env["GITHUB_TOKEN"] == secrets["GITHUB_TOKEN"]  # input untouched
    for value in secrets.values():
        assert all(value not in item for item in cleaned.values())
    assert cleaned == {"PATH": "/usr/bin", "UNRELATED": "nothing-secret-here"}


def test_empty_registry_leaves_text_and_environment_alone() -> None:
    registry = SecretRegistry()
    assert registry.redact("unchanged") == "unchanged"
    assert registry.scrub_env({"A": "b"}) == {"A": "b"}


def test_registry_built_from_the_environment_never_logs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_envtoken")
    monkeypatch.setenv("UNRELATED_VAR", "plain")
    registry = SecretRegistry.from_environment(["GITHUB_TOKEN", "MISSING", "UNRELATED_VAR"])
    assert registry.names == ("GITHUB_TOKEN", "UNRELATED_VAR")
    assert registry.redact("uses github_pat_envtoken") == "uses «redacted:GITHUB_TOKEN»"


def test_git_credential_helper_authenticates_without_a_token_url(tmp_path: Path) -> None:
    registry, secrets = _registry()
    token = secrets["GITHUB_TOKEN"]
    with temporary_git_credential_helper(token) as helper:
        args = helper.config_args()
        assert args[0] == "-c"
        assert token not in " ".join(args)
        assert "credential.helper=store" in args[1]
        stored = helper.store_file.read_text(encoding="utf-8")
        assert token in stored  # the file, not the argv, carries the secret
        mode = stat.S_IMODE(os.stat(helper.store_file).st_mode)
        assert mode == 0o600
        redacted_argv = registry.redact(" ".join(["git", *args, "ls-remote"]))
        assert token not in redacted_argv
        store_path = helper.store_file
    assert not store_path.exists()


def test_git_credential_helper_refuses_multiline_tokens() -> None:
    with pytest.raises(ValueError):
        with temporary_git_credential_helper("line-one\nline-two"):
            pass


def test_git_remote_url_never_carries_the_token() -> None:
    from coding_agent.runtime.git import target_repository_url

    url = target_repository_url("owner", "repo")
    assert url == "https://github.com/owner/repo.git"
    _, secrets = _registry()
    for value in secrets.values():
        assert value not in url


def test_git_push_through_the_helper_failure_carries_only_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coding_agent.runtime.git import GitFailure, Workspace, ensure_mirror

    registry, secrets = _registry()
    token = secrets["GITHUB_TOKEN"]
    argv_seen: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(
        args: Sequence[str], **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        argv_seen.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    with temporary_git_credential_helper(token) as helper:
        with pytest.raises(GitFailure) as excinfo:
            ensure_mirror(
                tmp_path / "mirror.git",
                "file:///nonexistent-remote/repo.git",
                helper=helper,
                redact=registry,
            )
    assert token not in str(excinfo.value)
    for argv in argv_seen:
        assert token not in " ".join(argv)


def test_scripted_attempt_with_every_secret_shows_only_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], skills_dir: Path
) -> None:
    """A scripted Attempt leaks no registered secret (contract §18, L3-ERR-11).

    The issue body, the tool arguments (``write_file`` content), the review
    findings, the model output and the model's own prints to both log streams
    all carry every registered secret; the Attempt runs with the registry as
    its one choke point. Afterwards both log streams, the transcript file and
    the ledger rows carry only ``«redacted:<name>»`` markers, and the git
    remote configuration carries no secret.
    """
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.messages import AIMessage, BaseMessage

    from conftest import init_origin_repo, run_git
    from support.fake_tracker import FakeTracker
    from support.scripted_model import ScriptedModel
    from support.skill_bundle import REVIEW_SKILL_TEXT
    from coding_agent.identity.startup import Identity
    from coding_agent.ledger.store import RunLedger
    from coding_agent.runtime.graph.attempt import run_attempt
    from coding_agent.runtime.transcript import TranscriptModel, TranscriptWriter
    from coding_agent.tracker.port import TrackedIssue

    secrets = {
        "GITHUB_TOKEN": "ghp_redactiontest_token_ABC123",
        "WORKER_ANTHROPIC_KEY": "sk-ant-redactiontest-key-DEF456",
        "OPENAI_API_KEY": "sk-redactiontest-key-GHI789",
    }
    registry = SecretRegistry(secrets)
    github_token = secrets["GITHUB_TOKEN"]
    anthropic_key = secrets["WORKER_ANTHROPIC_KEY"]
    openai_key = secrets["OPENAI_API_KEY"]

    profile = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_all.xml"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(profile, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    workspace = tmp_path / "workspace"
    run_git(["clone", "-q", str(bare), str(workspace)], cwd=tmp_path)

    (skills_dir / "code-review" / "SKILL.md").write_text(
        REVIEW_SKILL_TEXT, encoding="utf-8"
    )
    tracker = FakeTracker(
        issue=TrackedIssue(
            number=7,
            title="Add a greeting",
            body=(
                "## Acceptance criteria\n"
                "- [ ] `greet` returns a greeting\n"
                f"notes mention {github_token} {anthropic_key} {openai_key}\n"
            ),
        )
    )

    class LeakingModel(ScriptedModel):
        """A scripted model that also prints every secret to both streams."""

        def invoke(self, input: LanguageModelInput) -> BaseMessage:
            print(f"model stdout leak {github_token} {anthropic_key}")
            print(f"model stderr leak {openai_key}", file=sys.stderr)
            return super().invoke(input)

    model = LeakingModel(
        [
            AIMessage(
                content="activating",
                tool_calls=[
                    {
                        "name": "activate_implement",
                        "args": {},
                        "id": "call_activate_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content=f"working with {github_token}",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "path": "greeting.txt",
                            "content": f"hello {github_token}\nkey {anthropic_key}\nother {openai_key}\n",
                        },
                        "id": "call_write_1",
                        "type": "tool_call",
                    },
                    {
                        "name": "test_targeted",
                        "args": {"path": "tests/test_greet.py"},
                        "id": "call_test_1",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(
                content="reviewing",
                tool_calls=[
                    {
                        "name": "activate_code_review",
                        "args": {},
                        "id": "call_review_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="review seat",
                tool_calls=[
                    {
                        "name": "report_review",
                        "args": {"findings": f"no findings beyond {github_token}"},
                        "id": "seat-std-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="review seat",
                tool_calls=[
                    {
                        "name": "report_review",
                        "args": {"findings": f"clean {anthropic_key}"},
                        "id": "seat-spec-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content=f"The change is complete {openai_key}.",
                tool_calls=[
                    {
                        "name": "report_completion",
                        "args": {"AC-1": ["pkg::test_ok"]},
                        "id": "call_completion_1",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )

    evidence_dir = tmp_path / "evidence"
    transcript_path = evidence_dir / "transcript.jsonl"
    transcript_model = TranscriptModel(
        model, TranscriptWriter(transcript_path, registry)
    )
    ledger = RunLedger(tmp_path / "state" / "run-ledger.sqlite3", registry=registry)
    try:
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=transcript_model,
            workspace=workspace,
            effective_configuration={
                "repo": "example-owner/example-repo",
                "issue": 7,
                "provider": "anthropic",
                "target_language": "python",
            },
            skills_dir=skills_dir,
            evidence_dir=evidence_dir,
            remote_url=str(bare),
            agent_identity=Identity(account_id=999, login="coding-agent"),
            registry=registry,
        )
        ledger_text = "\n".join(
            [
                str(ledger.get_attempt("#7/1")),
                str(ledger.get_outcome("#7/1")),
                *(str(effect) for effect in ledger.list_effects("#7/1")),
            ]
        )
    finally:
        ledger.close()
    assert result.outcome == "delivered"

    captured = capsys.readouterr()
    _assert_no_secrets(captured.out, secrets)
    _assert_no_secrets(captured.err, secrets)
    # The leaking model prints the token and the Anthropic key to stdout and
    # the OpenAI key to stderr, so each stream carries its own markers.
    _assert_markers_present(
        captured.out, ["GITHUB_TOKEN", "WORKER_ANTHROPIC_KEY"]
    )
    _assert_markers_present(captured.err, ["OPENAI_API_KEY"])

    transcript_text = transcript_path.read_text(encoding="utf-8")
    _assert_no_secrets(transcript_text, secrets)
    _assert_markers_present(transcript_text, secrets)

    # The happy path stores no secret-bearing value in the ledger, so the
    # ledger leg is absence: no registered value in any row the Attempt
    # wrote. Secret-bearing rows carry markers instead (see
    # ``test_ledger_rows_carry_only_markers`` below).
    _assert_no_secrets(ledger_text, secrets)

    remote_output = run_git(["remote", "-v"], workspace)
    git_config = (workspace / ".git" / "config").read_text(encoding="utf-8")
    _assert_no_secrets(remote_output, secrets)
    _assert_no_secrets(git_config, secrets)


def test_ledger_rows_carry_only_markers(tmp_path: Path) -> None:
    """Secret-bearing ledger rows store markers, never values (§18, L3-ERR-11).

    The :class:`RunLedger` holds the redaction registry, so a Terminal
    Outcome recorded with every registered secret in its reason and bodies
    reads back with only ``«redacted:<name>»`` markers.
    """
    from coding_agent.ledger.store import RunLedger

    _, secrets = _registry()
    ledger = RunLedger(
        tmp_path / "run-ledger.sqlite3", registry=SecretRegistry(secrets)
    )
    try:
        stored = ledger.record_terminal_outcome(
            "#7/1",
            outcome="failed",
            reason=f"provider error {secrets['GITHUB_TOKEN']}",
            pull_request_body=f"body {secrets['WORKER_ANTHROPIC_KEY']}",
            terminal_comment_body=f"comment {secrets['OPENAI_API_KEY']}",
        )
        reread = ledger.get_outcome("#7/1")
    finally:
        ledger.close()
    assert reread is not None
    for row in (stored, reread):
        rendered = "\n".join(
            [row.reason, row.pull_request_body, row.terminal_comment_body]
        )
        _assert_no_secrets(rendered, secrets)
        _assert_markers_present(rendered, secrets)
