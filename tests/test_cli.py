import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent import cli
from coding_agent.implement import attempt, skeleton
from coding_agent.profile.toolchain import (
    SupportedToolchain,
    SupportedToolchainMatrix,
    load_toolchain_matrix,
)
from coding_agent.provider.config import PINNED_MODELS
from conftest import FakeChatModel, init_origin_repo, run_git


@pytest.fixture(autouse=True)
def _supported_toolchain_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = SupportedToolchainMatrix(
        schema=1,
        toolchains={"python": SupportedToolchain(version="3.13.9", package_manager_name="uv")},
    )
    monkeypatch.setattr(attempt, "_load_supported_toolchain_matrix", lambda _path: matrix)


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
    monkeypatch.setattr(
        attempt,
        "_load_supported_toolchain_matrix",
        lambda path: (_ for _ in ()).throw(FileNotFoundError(path)),
    )
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
    monkeypatch.setattr(attempt, "DEFAULT_TOOLCHAIN_MATRIX_PATH", tmp_path / "not-in-image.json")
    local_matrix = tmp_path / "tmp" / "toolchain-matrix.json"
    local_matrix.parent.mkdir()
    local_matrix.write_text("{}", encoding="utf-8")

    assert attempt.resolve_toolchain_matrix_path() == local_matrix


def test_run_implement_command_uses_the_local_matrix_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(attempt, "DEFAULT_TOOLCHAIN_MATRIX_PATH", tmp_path / "not-in-image.json")
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
    monkeypatch.setattr(
        attempt,
        "_load_supported_toolchain_matrix",
        lambda path: load_toolchain_matrix(json.loads(path.read_text(encoding="utf-8"))),
    )

    class ModelWasInitialized(Exception):
        pass

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


def _write_skills_home(base: Path) -> Path:
    """A minimal fixture standing in for an installed Skill Bundle, sized
    just enough for `compose_pinned_prefix` to read: the three `SKILL.md`
    files plus `tdd`'s two Companion Files."""
    skills_dir = base / ".agents" / "skills"
    for name in ("implement", "tdd", "codebase-design"):
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (skills_dir / "tdd" / "tests.md").write_text("# tests.md\n", encoding="utf-8")
    (skills_dir / "tdd" / "mocking.md").write_text("# mocking.md\n", encoding="utf-8")
    return base


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


def test_run_implement_command_reports_no_change_produced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    sha = run_git(["rev-parse", "HEAD"], origin)

    skills_home = _write_skills_home(tmp_path / "home")

    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(
        cli,
        "build_chat_model",
        lambda pin: FakeChatModel(
            [_capability_probe_response(), AIMessage(content="done", tool_calls=[])]
        ),
    )
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": body},
    )
    requests_mock.get(
        "https://api.github.com/user",
        json={"id": 999, "login": "coding-agent"},
        headers={"github-authentication-token-expiration": "2099-01-01 00:00:00 UTC"},
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        tmp_path / "state",
        skills_home,
        "python",
        "anthropic",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[PASS] issue fetched: #30 'a title'" in captured.out
    assert "[PASS] mirror updated:" in captured.out
    assert f"[PASS] workspace checked out: branch=main base_revision={sha}" in captured.out
    assert "[PASS] fingerprint computed:" in captured.out
    assert "[PASS] seam set confirmed: a_thing" in captured.out
    assert "[PASS] pinned prefix composed: 5 files injected;" in captured.out
    assert "[PASS] project profile read: language=python" in captured.out
    assert "[PASS] tool loop completed: 0 tool call(s)" in captured.out
    assert "[PASS] agent identity resolved: login=coding-agent" in captured.out
    assert "implement: no change produced; audit_evidence=" in captured.err
    assert "; no workspace diff" in captured.err
    assert (tmp_path / "state" / "workspaces" / "octocat" / "sandbox" / "README.md").exists()


def test_run_implement_command_delivers_and_validates_a_clean_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    skills_home = _write_skills_home(tmp_path / "home")

    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(
        cli,
        "build_chat_model",
        lambda pin: FakeChatModel(
            [
                _capability_probe_response(),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "thing.py", "content": "def thing():\n    return 42\n"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done", tool_calls=[]),
            ]
        ),
    )
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": body},
    )
    requests_mock.get(
        "https://api.github.com/user",
        json={"id": 999, "login": "coding-agent"},
        headers={"github-authentication-token-expiration": "2099-01-01 00:00:00 UTC"},
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        tmp_path / "state",
        skills_home,
        "python",
        "anthropic",
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[PASS] validate test_all:" in out
    assert out.count("[FAIL]") == 0
    assert "implement: verified completion; branch=agent/30/1-a-title sha=" in out


def test_run_implement_command_reports_validation_failed_on_a_new_regression(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's change makes `test_all` fail; the same command still
    passes at the Base Revision (read from the throwaway worktree), so this
    is a regression rather than an excused Baseline Failure."""
    origin = tmp_path / "origin"
    init_origin_repo(origin)
    (origin / "docs" / "agents").mkdir(parents=True)
    (origin / "docs" / "agents" / "project-profile.yml").write_text(_PROFILE_YAML, encoding="utf-8")
    (origin / "write_junit.py").write_text(
        """\
import os
import sys

path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
regressed = os.path.exists("regression-marker.py")
if regressed:
    body = '<testcase classname="pkg" name="ok"><failure message="boom"/></testcase>'
else:
    body = '<testcase classname="pkg" name="ok"/>'
with open(path, "w") as f:
    f.write(f'<testsuite name="suite" tests="1">{body}</testsuite>')
sys.exit(1 if regressed else 0)
""",
        encoding="utf-8",
    )
    run_git(["add", "."], origin)
    run_git(["commit", "-q", "-m", "add project profile"], origin)

    skills_home = _write_skills_home(tmp_path / "home")

    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(
        cli,
        "build_chat_model",
        lambda pin: FakeChatModel(
            [
                _capability_probe_response(),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "regression-marker.py", "content": "# marker\n"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done", tool_calls=[]),
            ]
        ),
    )
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": body},
    )
    requests_mock.get(
        "https://api.github.com/user",
        json={"id": 999, "login": "coding-agent"},
        headers={"github-authentication-token-expiration": "2099-01-01 00:00:00 UTC"},
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        tmp_path / "state",
        skills_home,
        "python",
        "anthropic",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[FAIL] validate test_all:" in captured.out
    assert "(regression)" in captured.out
    assert "implement: implemented but unverified; branch=agent/30/1-a-title sha=" in captured.err


def test_run_implement_command_refuses_when_the_provider_capability_assertion_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "build_chat_model", lambda pin: FakeChatModel([AIMessage(content="no tools", tool_calls=[])])
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
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "implement: provider-capability-refused:" in captured.err


def test_run_implement_command_reports_failed_limit_when_a_ceiling_stops_the_loop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _origin_with_profile(tmp_path)
    skills_home = _write_skills_home(tmp_path / "home")

    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(attempt, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(
        cli, "DEFAULT_EFFECTIVE_TOKEN_CEILINGS", {PINNED_MODELS["anthropic"].key: 1}
    )
    monkeypatch.setattr(
        cli,
        "build_chat_model",
        lambda pin: FakeChatModel(
            [
                _capability_probe_response(),
                AIMessage(
                    content="nothing to change here",
                    tool_calls=[],
                    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                ),
            ]
        ),
    )
    body = "a body\n\n## Acceptance criteria\n\n- [ ] `a_thing` is added.\n"
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": body},
    )
    requests_mock.get(
        "https://api.github.com/user",
        json={"id": 999, "login": "coding-agent"},
        headers={"github-authentication-token-expiration": "2099-01-01 00:00:00 UTC"},
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        30,
        "github_pat_testtoken",
        tmp_path / "state",
        skills_home,
        "python",
        "anthropic",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[FAIL] tool loop completed:" in captured.out
    assert "'tokens' ceiling" in captured.out
    assert "validate " not in captured.out  # no Delivery Snapshot exists to finalize
    assert "implement: no change produced; audit_evidence=" in captured.err


def test_run_implement_command_fails_clearly_on_an_unconfirmed_seam(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    init_origin_repo(origin)

    monkeypatch.setattr(skeleton, "remote_url", lambda owner, repo, token: str(origin))
    monkeypatch.setattr(
        cli, "build_chat_model", lambda pin: FakeChatModel([_capability_probe_response()])
    )
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/30",
        json={"number": 30, "title": "a title", "body": "a body with no seam"},
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
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[FAIL] seam set confirmed:" in captured.out
    assert "no public symbol, path or endpoint" in captured.out
    assert "implement: failed" in captured.err


def test_run_implement_command_fails_clearly_on_a_missing_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    requests_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "build_chat_model", lambda pin: FakeChatModel([_capability_probe_response()])
    )
    requests_mock.get(
        "https://api.github.com/repos/octocat/sandbox/issues/999",
        status_code=404,
        json={"message": "Not Found"},
    )

    exit_code = cli.run_implement_command(
        "octocat",
        "sandbox",
        999,
        "github_pat_testtoken",
        tmp_path / "state",
        tmp_path / "home",
        "python",
        "anthropic",
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "[FAIL] issue fetched:" in captured.out
    assert "implement: failed" in captured.err
