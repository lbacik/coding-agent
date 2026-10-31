"""PROTOTYPE (issue #73): the reference arm, unmodified.

Drives simple-coding-agent's own `ModelExecutor` (fb50952) -- the Claude
Agent SDK dispatching `/implement\\n\\n{body}`, its PreToolUse review counter
and its Stop-hook review nudge -- against the same fixture and model as the
LangGraph arm. Run with the reference's interpreter:

    /Volumes/Sources/python/ai/simple-coding-agent/.venv/bin/python run_reference.py [label]

HOME is pointed at a scratch directory holding only the five skills the
reference installs, so the CLI sees the deployment's bundle and nothing of
the operator's own ~/.claude. Reads ANTHROPIC_API_KEY from coding-agent's
`.env` (never printed).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).parent
REF = Path("/Volumes/Sources/python/ai/simple-coding-agent")
REPO = Path("/Volumes/Sources/python/ai/coding-agent")
SKILLS = Path.home() / ".agents" / "skills"
sys.path.insert(0, str(REF))

from simple_coding_agent.config import RuntimeConfig  # noqa: E402
from simple_coding_agent.model_execution import ModelExecutor  # noqa: E402

ISSUE = (HERE / "fixture/.issues/1.md").read_text()
BODY = ISSUE.split("\n", 1)[1].strip()


def env_value(name: str) -> str:
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{name} missing from .env")


def sh(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


class Archive:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write_text(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path


def main(label: str = "reference") -> None:
    root = HERE / ".work" / f"ref-{label}"
    if root.exists():
        shutil.rmtree(root)
    workspace = root / "workspace"
    shutil.copytree(HERE / "fixture", workspace)
    for args in (("git", "init", "-q", "-b", "main"), ("git", "add", "-A"),
                 ("git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture"),
                 ("git", "config", "user.name", "reference prototype"), ("git", "config", "user.email", "agent@example.invalid"),
                 ("git", "checkout", "-q", "-b", "agent/issue-1")):
        sh(*args, cwd=workspace).check_returncode()
    base = sh("git", "rev-parse", "main", cwd=workspace).stdout.strip()
    setup = sh("uv", "sync", "--frozen", cwd=workspace)  # the reference's `setup` step, run before the model
    assert setup.returncode == 0, setup.stderr

    home = root / "home"
    for skill in ("implement", "tdd", "code-review", "codebase-design"):
        shutil.copytree(SKILLS / skill, home / ".claude" / "skills" / skill, symlinks=False)
    shutil.copytree(REF / "skills" / "handoff", home / ".claude" / "skills" / "handoff")
    os.environ["HOME"] = str(home)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)

    events: list[dict] = []
    started = time.monotonic()
    config = RuntimeConfig(
        github_token="", meta_api_key="", target_repo="local/fixture", data_dir=root / "data", clone_dir=root / "clone",
        poll_interval=60, log_level="INFO", model_timeout=3600, publish_timeout=60, max_retries=0,
        max_consecutive_errors=3, agent_trust_project_settings=False, review_blocking_severities=frozenset(),
        model="claude-sonnet-5", model_base_url="", model_api_key=env_value("ANTHROPIC_API_KEY"),
        model_auth_mode="api_key", max_turns=60, max_budget_usd=10)
    executor = ModelExecutor(
        config,
        event_log=lambda event, detail="", level="INFO", issue_number=None: events.append(
            {"t": round(time.monotonic() - started, 2), "event": event, "detail": detail[:1500], "level": level}))
    executor.set_new_commits_provider(lambda: int(sh("git", "rev-list", "--count", f"{base}..HEAD", cwd=workspace).stdout.strip() or 0))
    error = None
    try:
        result = asyncio.run(executor.execute(issue_body=BODY, working_directory=workspace, issue_number=1,
                                              archive=Archive(root / "archive"), attempt_id="2026-10-31T00:00:00Z",
                                              branch="agent/issue-1", base_revision=base))
        result_dict = asdict(result)
    except Exception as exc:
        result_dict, error = {}, f"{type(exc).__name__}: {exc}"

    # The reference's own post-model gate: one `check` run, clean tree, commits, >=1 review.
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    check = sh("sh", "-c", f"uv run pytest -q --junit-xml={evidence}/test_all.xml", cwd=workspace)
    dirty = sh("git", "status", "--porcelain", cwd=workspace).stdout.strip()
    commits = sh("git", "log", "--format=%h %s", f"{base}..HEAD", cwd=workspace).stdout
    reviews = [e for e in result_dict.get("skill_events", ()) if e["name"] == "code-review" and e["phase"] == "PreToolUse"]
    record = {
        "arm": "reference", "model": "claude-sonnet-5", "error": error, "elapsed_s": round(time.monotonic() - started, 1),
        "status": result_dict.get("status"), "explanation": result_dict.get("explanation"),
        "terminal_reason": result_dict.get("terminal_reason"), "stop_reason": result_dict.get("stop_reason"),
        "model_usage": result_dict.get("model_usage"), "observed_models": result_dict.get("observed_models"),
        "skill_events": result_dict.get("skill_events"), "tool_calls": result_dict.get("tool_calls"),
        "review_invocations": len(reviews),
        "final_check": {"exit": check.returncode, "tail": check.stdout[-400:]}, "dirty": dirty, "git_log": commits,
        "diff": sh("git", "diff", base, "HEAD", cwd=workspace).stdout,
        "would_publish": bool(commits.strip()) and len(reviews) >= 1 and check.returncode == 0 and not dirty,
        "events": events,
    }
    (HERE / "transcripts" / f"ref-{label}.json").write_text(json.dumps(record, indent=2, default=str))
    print(json.dumps({k: record[k] for k in ("error", "elapsed_s", "status", "terminal_reason", "review_invocations",
                                             "final_check", "dirty", "git_log", "would_publish", "observed_models")}, indent=1, default=str))


if __name__ == "__main__":
    main(*sys.argv[1:])
