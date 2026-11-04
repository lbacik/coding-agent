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
from pathlib import Path

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
        args: object, **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        argv_seen.append(list(args))  # type: ignore[arg-type]
        return real_run(args, **kwargs)  # type: ignore[arg-type]

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
