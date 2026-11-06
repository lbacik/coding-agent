import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

"""The ``agent`` command line's argument mapping and dispatch.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (``agent run`` is the
Worker; a startup refusal opens no Run Ledger) and §18 (``preflight`` and
``startup-check``). The Worker's own behaviour is tested in
``test_worker_run.py``.

MVP row ids: P-WRK-3.
"""

from coding_agent import cli
from coding_agent.worker.supervisor import SkillBundleCheck
from coding_agent.worker.volume import VOLUME_LOCK_FILE_NAME
from coding_agent.ledger.store import RunLedger
from coding_agent.profile import toolchain as toolchain_module
from conftest import init_origin_repo, run_git
from support.scripted_model import ScriptedModel
from support.skill_bundle import REVIEW_SKILL_TEXT, write_skill_bundle
from test_worker_run import _assert_no_attempt_opened


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


def test_main_dispatches_to_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``agent run`` hands the Supervisor its paths; the Skill Bundle is
    verified against the reports in ``--skill-bundle-dir`` (contract §3)."""
    captured: dict[str, Any] = {}

    def fake_run_worker(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_worker", fake_run_worker)
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "run",
            "--config",
            "workflow.yml",
            "--workflow",
            "implement",
            "--state-dir",
            "state",
            "--skills-home",
            "/home/agent",
            "--skill-bundle-dir",
            "/opt/bundle",
            "--skill-bundle-ignore-file",
            "/opt/ignore.txt",
            "--toolchain-matrix",
            "/opt/matrix.json",
        ]
    )

    assert exit_code == 0
    assert captured["config"] == Path("workflow.yml")
    assert captured["workflow"] == "implement"
    assert captured["state_dir"] == tmp_path / "state"
    assert captured["skills_home"] == Path("/home/agent")
    assert captured["toolchain_matrix_path"] == Path("/opt/matrix.json")
    assert captured["skill_bundle"] == SkillBundleCheck(
        install_report=Path("/opt/bundle/install-report.json"),
        list_report=Path("/opt/bundle/list-report.json"),
        home=Path("/home/agent"),
        ignore_file=Path("/opt/ignore.txt"),
    )


def test_run_defaults_to_the_images_skill_bundle_and_state_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli, "run_worker", lambda **kwargs: captured.update(kwargs) or 0
    )

    assert cli.main(["run", "--config", "w.yml", "--workflow", "implement"]) == 0

    assert captured["state_dir"] == Path("/var/lib/coding-agent")
    bundle = captured["skill_bundle"]
    assert bundle.install_report == Path("/opt/coding-agent/skill-bundle/install-report.json")
    assert bundle.ignore_file == Path("/opt/coding-agent/skill-bundle-ignore.txt")


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--workflow", "implement"],
        ["run", "--config", "workflow.yml"],
    ],
)
def test_run_requires_config_and_workflow(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_implement_is_gone() -> None:
    """``agent implement`` retired with M6 (issue #153): ``run`` replaces it."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["implement", "--issue", "1"])
    assert excinfo.value.code == 2


def test_run_refuses_a_missing_toolchain_matrix_before_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3: a refusal opens no Run Ledger — only the step-2 lock file exists.

    The exclusive volume lock is startup step 2, so the state directory and
    its lock file already exist when the missing matrix refuses; what must
    not exist is the Run Ledger (W-CFG-14).
    """
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = tmp_path / "workflow.yml"
    config.write_text(_RUN_CONFIG, encoding="utf-8")
    state_dir = tmp_path / "state"

    exit_code = cli.main(
        [
            "run",
            "--config",
            str(config),
            "--workflow",
            "implement",
            "--state-dir",
            str(state_dir),
            "--toolchain-matrix",
            str(tmp_path / "missing-matrix.json"),
        ]
    )

    assert exit_code == 1
    assert "Supported Toolchain Matrix" in capsys.readouterr().err
    assert (state_dir / VOLUME_LOCK_FILE_NAME).exists()
    # Adoption step 4 (#248): the operator store opens right after the
    # volume lock in the ledger's SQLite file, so a later refusal leaves
    # the file behind with only operator tables. What must still hold is
    # P-WRK-3: no Attempt row.
    _assert_no_attempt_opened(state_dir / "run-ledger.sqlite3")


_RUN_CONFIG = """\
version: 1

target:
  kind: github
  repository: octocat/sandbox
  base_branch: main
  token_env: GITHUB_TOKEN

models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}

workflows:
  implement:
    model: sonnet
    selector:
      label: ready-for-agent
    steps:
      - id: implement
        skill: implement
"""



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


def test_main_loads_default_dotenv_and_app_env_file_before_the_run_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PyCharm configuration keeps shared values in `.env` and adds its database URL.

    The app-specific file is loaded ahead of `run` so it has `DATABASE_URL` before
    environment readiness is evaluated."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from-default-file\n")
    env_file = tmp_path / "custom.env"
    env_file.write_text("DATABASE_URL=postgres://example\n")

    monkeypatch.setattr(cli, "run_run_command", lambda *args, **kwargs: 0)

    exit_code = cli.main(
        [
            "--app-env-file",
            str(env_file),
            "run",
            "--config",
            "workflow.yml",
            "--workflow",
            "implement",
        ]
    )

    assert exit_code == 0
    assert os.environ["GITHUB_TOKEN"] == "from-default-file"
    assert os.environ["DATABASE_URL"] == "postgres://example"



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
