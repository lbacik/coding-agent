import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

"""The ``agent implement`` command runs the Attempt graph over the ``local`` tracker.

Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Claim), §7
(readiness and the baseline before any model call), §14 (the Terminal
Outcome is recorded before it is printed) and §17 (the Run Ledger file).

MVP row ids: L1-6, P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4, C-RDY-1, C-RDY-2,
C-RDY-3, C-RDY-4, L2-1, L2-12, L2-13, L2-14.
"""

from coding_agent import cli
from coding_agent.ledger.store import RunLedger
from coding_agent.profile import toolchain as toolchain_module
from conftest import init_origin_repo, run_git
from support.scripted_model import ScriptedModel
from support.skill_bundle import write_skill_bundle


def test_split_repo_valid() -> None:
    assert cli.split_repo("octocat/sandbox") == ("octocat", "sandbox")


@pytest.mark.parametrize("value", ["", "no-slash", "/repo", "owner/"])
def test_split_repo_rejects_malformed(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.split_repo(value)


def test_resolve_state_dir_leaves_absolute_path_unchanged() -> None:
    assert cli.resolve_state_dir("/tmp/state") == Path("/tmp/state")


def test_resolve_state_dir_resolves_relative_path_against_cwd() -> None:
    assert cli.resolve_state_dir("tmp/state") == Path.cwd() / "tmp" / "state"


def test_load_token_missing_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_TOKEN_VAR", raising=False)
    with pytest.raises(SystemExit):
        cli.load_token("SOME_TOKEN_VAR")


def test_load_token_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_TOKEN_VAR", "github_pat_abc")
    assert cli.load_token("SOME_TOKEN_VAR") == "github_pat_abc"


def test_main_dispatches_to_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[tuple[str, str, str]] = []

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        calls.append((owner, repo, token))
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    exit_code = cli.main(["preflight", "--repo", "octocat/sandbox"])

    assert exit_code == 0
    assert calls == [("octocat", "sandbox", "github_pat_abc")]


def test_main_dispatches_to_startup_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_TOKEN", "github_pat_abc")
    calls: list[tuple[str, str, str]] = []

    def fake_run_startup_check_command(owner: str, repo: str, token: str) -> int:
        calls.append((owner, repo, token))
        return 1

    monkeypatch.setattr(cli, "run_startup_check_command", fake_run_startup_check_command)

    exit_code = cli.main(
        ["startup-check", "--repo", "octocat/sandbox", "--token-env", "SANDBOX_TOKEN"]
    )

    assert exit_code == 1
    assert calls == [("octocat", "sandbox", "github_pat_abc")]


def test_main_dispatches_to_implement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[tuple[str, str, int, str, Path, Path, str, str, str, int]] = []

    def fake_run_implement_command(
        owner: str,
        repo: str,
        issue: int,
        token: str,
        state_dir: Path,
        skills_home: Path,
        target_language: str,
        provider: str,
        token_env: str,
        attempt_number: int,
        *,
        toolchain_matrix_path: Path | None = None,
    ) -> int:
        calls.append(
            (
                owner,
                repo,
                issue,
                token,
                state_dir,
                skills_home,
                target_language,
                provider,
                token_env,
                attempt_number,
            )
        )
        return 0

    monkeypatch.setattr(cli, "run_implement_command", fake_run_implement_command)

    exit_code = cli.main(
        [
            "implement",
            "--repo",
            "octocat/sandbox",
            "--issue",
            "30",
            "--target-language",
            "python",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "octocat",
            "sandbox",
            30,
            "github_pat_abc",
            Path("/var/lib/coding-agent"),
            Path.home(),
            "python",
            "anthropic",
            "GITHUB_TOKEN",
            1,
        )
    ]


def test_main_dispatches_to_implement_with_a_custom_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[Path] = []

    def fake_run_implement_command(
        owner: str,
        repo: str,
        issue: int,
        token: str,
        state_dir: Path,
        skills_home: Path,
        target_language: str,
        provider: str,
        token_env: str,
        attempt_number: int,
        *,
        toolchain_matrix_path: Path | None = None,
    ) -> int:
        calls.append(state_dir)
        return 0

    monkeypatch.setattr(cli, "run_implement_command", fake_run_implement_command)

    cli.main(
        [
            "implement",
            "--repo",
            "octocat/sandbox",
            "--issue",
            "30",
            "--state-dir",
            "/tmp/state",
            "--target-language",
            "python",
        ]
    )

    assert calls == [Path("/tmp/state")]


def test_main_dispatches_to_implement_resolves_a_relative_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[Path] = []

    def fake_run_implement_command(
        owner: str,
        repo: str,
        issue: int,
        token: str,
        state_dir: Path,
        skills_home: Path,
        target_language: str,
        provider: str,
        token_env: str,
        attempt_number: int,
        *,
        toolchain_matrix_path: Path | None = None,
    ) -> int:
        calls.append(state_dir)
        return 0

    monkeypatch.setattr(cli, "run_implement_command", fake_run_implement_command)

    cli.main(
        [
            "implement",
            "--repo",
            "octocat/sandbox",
            "--issue",
            "30",
            "--state-dir",
            "tmp/coding-agent-state",
            "--target-language",
            "python",
        ]
    )

    assert calls == [Path.cwd() / "tmp" / "coding-agent-state"]


def test_main_dispatches_to_implement_with_a_toolchain_matrix_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_abc")
    calls: list[dict[str, object]] = []

    def fake_run_implement_command(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_implement_command", fake_run_implement_command)

    cli.main(
        [
            "implement",
            "--repo",
            "octocat/sandbox",
            "--issue",
            "30",
            "--target-language",
            "python",
            "--toolchain-matrix",
            "local/toolchain-matrix.json",
        ]
    )

    assert calls == [{"toolchain_matrix_path": Path.cwd() / "local/toolchain-matrix.json"}]


def test_run_implement_command_reports_missing_toolchain_matrix_before_creating_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_chat_model", lambda _pin: pytest.fail("model was built"))
    matrix_path = tmp_path / "missing-toolchain-matrix.json"
    state_dir = tmp_path / "state"

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        tmp_path / "home",
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    assert "Supported Toolchain Matrix" in capsys.readouterr().err
    assert not state_dir.exists()


def test_resolve_toolchain_matrix_path_uses_the_local_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        toolchain_module, "DEFAULT_TOOLCHAIN_MATRIX_PATH", tmp_path / "not-in-image.json"
    )
    local_matrix = tmp_path / "tmp" / "toolchain-matrix.json"
    local_matrix.parent.mkdir()
    local_matrix.write_text("{}", encoding="utf-8")

    assert toolchain_module.resolve_toolchain_matrix_path() == local_matrix


def test_run_implement_command_uses_the_local_matrix_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        toolchain_module, "DEFAULT_TOOLCHAIN_MATRIX_PATH", tmp_path / "not-in-image.json"
    )
    local_matrix = tmp_path / "tmp" / "toolchain-matrix.json"
    local_matrix.parent.mkdir()
    local_matrix.write_text(
        json.dumps(
            {
                "schema": 1,
                "toolchains": {
                    "python": {
                        "version": "3.13.1",
                        "package_manager": {"name": "uv", "version": "0.12.8"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    class ModelWasInitialized(Exception):
        pass

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "assert_provider_capability", lambda pin, model, table: None)
    monkeypatch.setattr(
        cli, "build_chat_model", lambda _pin: (_ for _ in ()).throw(ModelWasInitialized())
    )

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    with pytest.raises(ModelWasInitialized):
        cli.main(
            [
                "implement",
                "--repo",
                "octocat/sandbox",
                "--issue",
                "30",
                "--target-language",
                "python",
            ]
        )


def test_main_dispatches_to_verify_skill_bundle_without_repo_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls: list[tuple[Path, Path, Path, Path]] = []

    def fake_run_verify_skill_bundle_command(
        install_report: Path, list_report: Path, home: Path, ignore_file: Path
    ) -> int:
        calls.append((install_report, list_report, home, ignore_file))
        return 0

    monkeypatch.setattr(
        cli, "run_verify_skill_bundle_command", fake_run_verify_skill_bundle_command
    )

    exit_code = cli.main(
        [
            "verify-skill-bundle",
            "--install-report",
            "install.json",
            "--list-report",
            "list.json",
            "--home",
            "/home/agent",
            "--ignore-file",
            "ignore.txt",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (Path("install.json"), Path("list.json"), Path("/home/agent"), Path("ignore.txt"))
    ]


def test_main_dispatches_to_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, Path | None, Path, bool, str | None]] = []

    def fake_run_validate_command(
        project_dir: Path,
        profile_path: Path | None,
        evidence_dir: Path,
        *,
        skip_bootstrap: bool,
        targeted: str | None,
    ) -> int:
        calls.append((project_dir, profile_path, evidence_dir, skip_bootstrap, targeted))
        return 0

    monkeypatch.setattr(cli, "run_validate_command", fake_run_validate_command)

    exit_code = cli.main(
        [
            "validate",
            "--project-dir",
            "proj",
            "--evidence-dir",
            "evidence",
            "--targeted",
            "tests/test_a.py",
        ]
    )

    assert exit_code == 0
    assert calls == [(Path("proj"), None, Path("evidence"), False, "tests/test_a.py")]


def test_main_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_build_parser_accepts_app_env_file_before_the_subcommand() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(
        ["--app-env-file", "./tmp/x.env", "preflight", "--repo", "octocat/sandbox"]
    )

    assert args.app_env_file == Path("./tmp/x.env")


def test_main_loads_an_app_env_file_before_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "custom.env"
    env_file.write_text("GITHUB_TOKEN=from-custom-file\n")

    calls: list[str] = []

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        calls.append(token)
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    exit_code = cli.main(
        ["--app-env-file", str(env_file), "preflight", "--repo", "octocat/sandbox"]
    )

    assert exit_code == 0
    assert calls == ["from-custom-file"]


def test_main_app_env_file_does_not_override_an_existing_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "already-set")
    env_file = tmp_path / "custom.env"
    env_file.write_text("GITHUB_TOKEN=from-custom-file\n")

    calls: list[str] = []

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        calls.append(token)
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    exit_code = cli.main(
        ["--app-env-file", str(env_file), "preflight", "--repo", "octocat/sandbox"]
    )

    assert exit_code == 0
    assert calls == ["already-set"]


def test_main_reports_a_missing_env_file_as_a_clear_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def fake_run_preflight_command(owner: str, repo: str, token: str) -> int:
        calls.append(token)
        return 0

    monkeypatch.setattr(cli, "run_preflight_command", fake_run_preflight_command)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "--app-env-file",
                str(tmp_path / "does-not-exist.env"),
                "preflight",
                "--repo",
                "octocat/sandbox",
            ]
        )

    assert excinfo.value.code != 0
    assert calls == []
    assert "env file not found" in capsys.readouterr().err


def test_main_loads_default_dotenv_and_app_env_file_before_the_implement_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PyCharm configuration keeps shared values in `.env` and adds its database URL.

    The app-specific file is loaded ahead of `implement` so it has `DATABASE_URL` before
    environment readiness is evaluated."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from-default-file\n")
    env_file = tmp_path / "custom.env"
    env_file.write_text("DATABASE_URL=postgres://example\n")

    monkeypatch.setattr(cli, "run_implement_command", lambda *args, **kwargs: 0)

    exit_code = cli.main(
        [
            "--app-env-file",
            str(env_file),
            "implement",
            "--repo",
            "octocat/sandbox",
            "--issue",
            "30",
            "--target-language",
            "python",
        ]
    )

    assert exit_code == 0
    assert os.environ["GITHUB_TOKEN"] == "from-default-file"
    assert os.environ["DATABASE_URL"] == "postgres://example"
    del os.environ["GITHUB_TOKEN"]
    del os.environ["DATABASE_URL"]


# --- run_validate_command: real subprocess execution end to end ------------


def _write_profile(project_dir: Path, *, working_directory: str = ".") -> None:
    profile_dir = project_dir / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text(
        f"""
schema: 2
language: python
working_directory: {working_directory}
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: python3 -c "pass"
  test_all: >-
    python3 write_junit.py {{evidence_dir}}/test_all.xml A=pass B=fail
  test_targeted: >-
    python3 write_junit.py {{evidence_dir}}/test_targeted.xml {{path}}
evidence:
  format: junit-xml
  test_all: "{{evidence_dir}}/test_all.xml"
  test_targeted: "{{evidence_dir}}/test_targeted.xml"
checks: none
""",
        encoding="utf-8",
    )


_WRITE_JUNIT_SCRIPT = """
import os
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
cases = sys.argv[2:]
parts = [f'<testsuite name="suite" tests="{len(cases)}">']
failed = False
for case in cases:
    name, outcome = case.split("=")
    if outcome == "fail":
        parts.append(f'<testcase classname="pkg" name="{name}"><failure message="boom"/></testcase>')
        failed = True
    else:
        parts.append(f'<testcase classname="pkg" name="{name}"/>')
parts.append("</testsuite>")
with open(path, "w") as f:
    f.write("".join(parts))
sys.exit(1 if failed else 0)
"""


def test_run_validate_command_runs_bootstrap_and_test_all_for_real(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "write_junit.py").write_text(_WRITE_JUNIT_SCRIPT, encoding="utf-8")
    _write_profile(project_dir)
    evidence_dir = tmp_path / "evidence"

    exit_code = cli.run_validate_command(
        project_dir, None, evidence_dir, skip_bootstrap=False, targeted=None
    )

    assert exit_code == 1  # test_all names one real failure (B)
    out = capsys.readouterr().out
    assert "[PASS] bootstrap: command='python3 -c \"pass\"'" in out
    assert "executed=2 failures=['pkg::B']" in out
    assert out.count("[FAIL] test_all:") == 1


def test_run_validate_command_targeted_runs_only_the_named_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "write_junit.py").write_text(_WRITE_JUNIT_SCRIPT, encoding="utf-8")
    _write_profile(project_dir)
    evidence_dir = tmp_path / "evidence"

    exit_code = cli.run_validate_command(
        project_dir, None, evidence_dir, skip_bootstrap=True, targeted="A=pass"
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[PASS] test_targeted:" in out
    assert "exit=0 executed=1 failures=[]" in out


def test_run_validate_command_reports_a_profile_that_is_not_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_dir = tmp_path / "project"
    profile_dir = project_dir / "docs" / "agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "project-profile.yml").write_text("schema: 2\n", encoding="utf-8")

    exit_code = cli.run_validate_command(
        project_dir, None, tmp_path / "evidence", skip_bootstrap=True, targeted=None
    )

    assert exit_code == 1
    assert "profile is not ready" in capsys.readouterr().err


# --- run_implement_command: real git mirror/checkout, faked GitHub client --


_PROFILE_YAML = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: python3 write_junit.py {evidence_dir}/test_all.xml
  test_targeted: python3 write_junit.py {evidence_dir}/test_targeted.xml
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
"""

_IMPLEMENT_WRITE_JUNIT_SCRIPT = """\
import os
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write('<testsuite name="suite" tests="1"><testcase classname="pkg" name="ok"/></testsuite>')
"""


def _capability_probe_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "capability_probe", "args": {"city": "paris"}, "id": "probe-1", "type": "tool_call"}
        ],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def _origin_with_profile(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    (origin / "docs" / "agents").mkdir(parents=True)
    (origin / "docs" / "agents" / "project-profile.yml").write_text(
        _PROFILE_YAML, encoding="utf-8"
    )
    (origin / "write_junit.py").write_text(_IMPLEMENT_WRITE_JUNIT_SCRIPT, encoding="utf-8")
    run_git(["add", "."], origin)
    run_git(["commit", "-q", "-m", "add project profile"], origin)
    return origin


def _write_matrix_file(tmp_path: Path) -> Path:
    """A real Supported Toolchain Matrix file satisfying the test profile."""
    matrix_path = tmp_path / "toolchain-matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "toolchains": {
                    "python": {
                        "version": "3.13.9",
                        "package_manager": {"name": "uv", "version": "0.5.29"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return matrix_path


def _mock_issue(
    requests_mock: Any, *, number: int = 30, title: str = "a title", body: str
) -> None:
    requests_mock.get(
        f"https://api.github.com/repos/octocat/sandbox/issues/{number}",
        json={"number": number, "title": title, "body": body},
    )


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {
                "name": "activate_implement",
                "args": {},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _working_message() -> AIMessage:
    return AIMessage(
        content="working",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "greeting.txt", "content": "hello\n"},
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
    )


def _completion_message() -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {
                "name": "report_completion",
                "args": {"AC-1": ["pkg::ok"]},
                "id": "call_completion_1",
                "type": "tool_call",
            }
        ],
    )


def _no_capability_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Provider Capability Assertion is M5's row (L1-4); these M1 command
    tests hold it fixed so a scripted model proves the Attempt graph alone."""
    monkeypatch.setattr(cli, "assert_provider_capability", lambda pin, model, table: None)


def _mock_startup_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the Agent Identity fixed: ``implement`` resolves it like
    ``startup-check`` before any model call (contract §10)."""
    from datetime import datetime, timedelta, timezone

    from coding_agent.identity.startup import Identity, StartupCheckResult
    from coding_agent.identity.token import TokenExpiration

    result = StartupCheckResult(
        identity=Identity(account_id=12345, login="octocat"),
        token_expiration=TokenExpiration(
            expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            remaining=timedelta(days=365),
        ),
        push_allowed=True,
        rate_limit_remaining=4999,
        rate_limit_limit=5000,
    )
    monkeypatch.setattr(cli, "run_startup_checks", lambda *args, **kwargs: result)


def test_implement_runs_the_attempt_graph_records_the_outcome_and_prints_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel([_activation_message(), _working_message(), _completion_message()])

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    _mock_issue(requests_mock, body=body)

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "implement: delivered (verified-completion); attempt=#30/1" in out
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        outcome = ledger.get_outcome("#30/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("delivered", "verified-completion")
        attempt = ledger.get_attempt("#30/1")
        assert attempt.state == "outcome-recorded"
        assert attempt.effective_configuration["endpoint"] == "messages"
        assert (
            attempt.effective_configuration["provider"]
            == "anthropic:claude-sonnet-5@messages"
        )
        from coding_agent.runtime.fingerprint import compute_fingerprint

        assert attempt.fingerprint == compute_fingerprint("a title", body)
    finally:
        ledger.close()
    assert model.invocations, "the Attempt graph never called the model"
    assert "report_completion" in model.bound_tool_names
    assert {request.method for request in requests_mock.request_history} == {"GET"}


def test_implement_missing_readiness_fact_ends_before_any_model_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel([])

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    _mock_issue(requests_mock, body="a body with no checklist")

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    assert model.invocations == []
    out = capsys.readouterr().out
    assert "implement: failed (missing-readiness-fact:" in out
    assert "attempt=#30/1" in out
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        outcome = ledger.get_outcome("#30/1")
        assert outcome is not None
        assert outcome.outcome == "failed"
        assert outcome.reason.startswith("missing-readiness-fact:")
        assert f"implement: {outcome.outcome} ({outcome.reason})" in out
    finally:
        ledger.close()
    assert {request.method for request in requests_mock.request_history} == {"GET"}


def test_implement_red_baseline_ends_before_any_model_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    (origin / "docs" / "agents").mkdir(parents=True)
    (origin / "docs" / "agents" / "project-profile.yml").write_text(
        _PROFILE_YAML, encoding="utf-8"
    )
    (origin / "write_junit.py").write_text(
        """\
import os
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write(
        '<testsuite name="suite" tests="1">'
        '<testcase classname="pkg" name="ok">'
        '<failure message="boom"/>'
        "</testcase></testsuite>"
    )
sys.exit(1)
""",
        encoding="utf-8",
    )
    run_git(["add", "."], origin)
    run_git(["commit", "-q", "-m", "add project profile"], origin)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel([])

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    _mock_issue(requests_mock, body=body)

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    assert model.invocations == []
    out = capsys.readouterr().out
    assert "implement: failed (red-baseline: test_all" in out
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        outcome = ledger.get_outcome("#30/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason.split(";")[0]) == (
            "failed",
            "red-baseline: test_all",
        )
    finally:
        ledger.close()


def test_implement_refuses_when_the_provider_capability_assertion_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix_path = _write_matrix_file(tmp_path)
    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(
        cli,
        "build_chat_model",
        lambda pin: ScriptedModel([AIMessage(content="no tools", tool_calls=[])]),
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        tmp_path / "state",
        tmp_path / "home",
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "implement: provider-capability-refused:" in captured.err


def test_implement_fails_clearly_on_a_missing_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel([])

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        999,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    assert model.invocations == []
    out = capsys.readouterr().out
    assert "implement: failed (unclassified-error); attempt=#999/1" in out
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        outcome = ledger.get_outcome("#999/1")
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("failed", "unclassified-error")
    finally:
        ledger.close()


def _review_completion(call_id: str) -> AIMessage:
    return AIMessage(
        content="The change is complete.",
        tool_calls=[
            {"name": "report_completion", "args": {"AC-1": ["pkg::ok"]}, "id": call_id, "type": "tool_call"}
        ],
    )


def test_implement_passes_remote_and_identity_to_run_attempt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10/§13/§18: ``implement`` binds the credentialed remote and identity.

    The Agent Identity resolves like ``startup-check`` before any model call,
    and the credentialed remote plus identity reach ``run_attempt`` so
    ``activate_code_review`` runs the snapshot push and both seats.
    """
    origin = _origin_with_profile(tmp_path)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel([_activation_message(), _working_message(), _review_completion("call_completion_1")])

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    _mock_issue(requests_mock, body=body)

    real_run_attempt = cli.run_attempt
    captured: dict[str, object] = {}

    def spying_run_attempt(*args: Any, **kwargs: Any) -> Any:
        captured["remote_url"] = kwargs.get("remote_url")
        captured["agent_identity"] = kwargs.get("agent_identity")
        captured["review_redact"] = kwargs.get("review_redact")
        return real_run_attempt(*args, **kwargs)

    monkeypatch.setattr(cli, "run_attempt", spying_run_attempt)

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 0
    assert captured["remote_url"] == str(origin)
    from coding_agent.identity.startup import Identity

    identity = captured["agent_identity"]
    assert isinstance(identity, Identity)
    assert identity.login == "octocat"
    assert captured["review_redact"] == "github_pat_testtoken"


def test_implement_review_snapshot_pushes_branch_against_local_bare_remote(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10/§13/§18: ``activate_code_review`` pushes ``agent/<issue>/<n>-<slug>``.

    A live-model shaped run against a local bare remote: the snapshot push
    runs first and both reviewer seats run after its receipt.
    """
    import subprocess

    from langchain_core.messages import UsageMetadata

    def _ai_with_usage(
        *, content: str = "", calls: list[dict[str, object]] | None = None
    ) -> AIMessage:
        usage: UsageMetadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        return AIMessage(content=content, tool_calls=list(calls or []), usage_metadata=usage)

    def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
        return {"name": name, "args": args, "id": call_id, "type": "tool_call"}

    origin = _origin_with_profile(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(bare)], check=True)
    matrix_path = _write_matrix_file(tmp_path)
    home = tmp_path / "home"
    write_skill_bundle(home)
    model = ScriptedModel(
        [
            _activation_message(),
            _working_message(),
            AIMessage(
                content="reviewing",
                tool_calls=[_call("activate_code_review", {}, "call_review_1")],
            ),
            _ai_with_usage(
                content="standards",
                calls=[_call("report_review", {"findings": "standards: ok"}, "seat-std-1")],
            ),
            _ai_with_usage(
                content="spec",
                calls=[_call("report_review", {"findings": "spec: ok"}, "seat-spec-1")],
            ),
            AIMessage(
                content="addressing review feedback",
                tool_calls=[
                    _call(
                        "write_file",
                        {"path": "greeting.txt", "content": "hello, reviewed\n"},
                        "call_write_2",
                    ),
                    _call("test_targeted", {"path": "tests/test_greet.py"}, "call_test_2"),
                ],
            ),
            _review_completion("call_completion_1"),
        ]
    )

    _mock_startup_success(monkeypatch)
    monkeypatch.setattr(cli, "remote_url", lambda owner, repo, token: str(bare))
    monkeypatch.setattr(cli, "build_chat_model", lambda pin: model)
    _no_capability_assertion(monkeypatch)
    title = "Fix the loop!"
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": title, "body": body},
    )

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        state_dir,
        home,
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 0
    branch = "agent/30/1-fix-the-loop"
    bare_sha = run_git(["rev-parse", branch], bare)
    assert len(bare_sha) == 40
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        from coding_agent.runtime.review_snapshot import REVIEW_SNAPSHOT_KIND_PREFIX

        effect = ledger.get_effect("#30/1", f"{REVIEW_SNAPSHOT_KIND_PREFIX}1")
        assert effect is not None
        assert effect.status == "receipt"
        assert effect.remote_id == bare_sha
    finally:
        ledger.close()


def test_implement_rejected_token_ends_before_attempt_or_model_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§10: a rejected token ends the command non-zero with no write.

    ``resolve_identity`` runs before the first model call; the Attempt never
    opens and the model is never built.
    """
    from coding_agent.identity.token import TokenRejected

    matrix_path = _write_matrix_file(tmp_path)

    def _rejecting_startup(*args: Any, **kwargs: Any) -> Any:
        raise TokenRejected("credential must use the 'github_pat_' prefix")

    monkeypatch.setattr(cli, "run_startup_checks", _rejecting_startup)

    def _fail_on_model(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the model must not be built after a rejected token")

    monkeypatch.setattr(cli, "build_chat_model", _fail_on_model)

    state_dir = tmp_path / "state"
    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "ghp_classic",
        state_dir,
        tmp_path / "home",
        "python",
        "anthropic",
        toolchain_matrix_path=matrix_path,
    )

    assert exit_code == 1
    assert "startup-check FAILED" in capsys.readouterr().err
    assert not (state_dir / "run-ledger.sqlite3").exists()


def test_implement_invokes_the_attempt_graph_not_the_retired_orchestration() -> None:
    import inspect

    for module_name in (
        "coding_agent.runtime.attempt",
        "coding_agent.runtime.skeleton",
        "coding_agent.runtime.loop",
    ):
        try:
            __import__(module_name)
        except ImportError:
            pass
        else:
            raise AssertionError(f"retired orchestration module still importable: {module_name}")
    source = inspect.getsource(cli.run_implement_command)
    for retired in (
        "run_implement_attempt",
        "AttemptReport",
        "run_tool_loop",
        "build_opening_messages",
    ):
        assert retired not in source, retired
    assert "run_attempt" in source


