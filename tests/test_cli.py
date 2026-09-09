import argparse
from pathlib import Path

import pytest

from coding_agent import cli


def test_split_repo_valid() -> None:
    assert cli.split_repo("octocat/sandbox") == ("octocat", "sandbox")


@pytest.mark.parametrize("value", ["", "no-slash", "/repo", "owner/"])
def test_split_repo_rejects_malformed(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.split_repo(value)


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
