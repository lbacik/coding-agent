"""Oracle-access detection: Transcript per run, scan, voiding, reporting."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.experiment.harness import (
    STATUS_VOIDED,
    ExperimentDeps,
    build_aggregate,
    run_experiment,
)
from coding_agent.experiment.oracle_access import (
    FIXTURES_SIGNAL,
    REPO_SIGNAL,
    build_oracle_access_check,
    oracle_access_signals,
    scan_transcript,
)
from coding_agent.experiment.protocol import issue_body_digest, parse_protocol
from coding_agent.provider.pinned_model import InvokableToolModel
from conftest import init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION: dict[str, object] = {
    "repo": "example-owner/example-repo",
    "issue": 7,
    "provider": "anthropic",
    "target_language": "python",
}

HAPPY_PROFILE = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "mkdir -p {evidence_dir} && printf '%s' '<testsuite name=\\"suite\\" tests=\\"1\\"><testcase classname=\\"pkg\\" name=\\"test_ok\\"/></testsuite>' > {evidence_dir}/test_targeted.xml && true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_targeted.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

ORACLE_FILES = {"oracle_probe.txt": "probe-ok\n"}
ORACLE_COMMAND = ["cat", "oracle_probe.txt"]
PINNED_MODEL = {"provider": "anthropic", "model": "test-model", "effort": "high"}


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _silent_message(text: str = "still thinking") -> AIMessage:
    return AIMessage(content=text, tool_calls=[])


def _tool_message(name: str, args: dict[str, object], call_id: str) -> AIMessage:
    return AIMessage(
        content="calling",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _repo(tmp_path: Path, name: str) -> tuple[Path, str]:
    src = tmp_path / f"{name}-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
    (src / "greeting.txt").write_text("hello\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "base"], src)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    workspace = tmp_path / f"{name}-work"
    run_git(["clone", "-q", str(src), str(workspace)], cwd=tmp_path)
    return workspace, base


def _protocol_document(digest: str, base: str, runs: int = 1) -> dict[str, Any]:
    order = [{"arm": "contract", "fixture": "fA", "run": i + 1} for i in range(runs)]
    return {
        "schema_version": 1,
        "arms": [{"id": "contract", "exit_policy": "contract"}],
        "fixtures": [
            {
                "id": "fA",
                "issue_number": 7,
                "issue_body_digest": digest,
                "base_revision": base,
                "oracle": list(ORACLE_COMMAND),
                "oracle_files": dict(ORACLE_FILES),
            }
        ],
        "runs_per_cell": runs,
        "order": order,
        "per_attempt_cost_usd": 5.0,
        "overall_cap_usd": 1000.0,
        "pinned_model": dict(PINNED_MODEL),
    }


def _deps(
    tmp_path: Path,
    workspace: Path,
    skills_dir: Path,
    make_model: Callable[[str, str, int], InvokableToolModel],
) -> ExperimentDeps:
    return ExperimentDeps(
        tracker=FakeTracker(),
        workspace=workspace,
        skills_dir=skills_dir,
        out_dir=tmp_path / "experiment-out",
        effective_configuration=dict(EFFECTIVE_CONFIGURATION),
        make_model=make_model,
        runtime_commit="test-commit",
        skill_bundle_digest="test-skills-digest",
        oracle_timeout_seconds=60.0,
    )


def _transcript_records(out_dir: Path, seq: int) -> list[dict[str, Any]]:
    path = out_dir / "evidence" / f"run-{seq:03d}" / "transcript.jsonl"
    assert path.is_file(), f"missing transcript {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_harness_writes_transcript_like_transcript_model(
    tmp_path: Path, skills_dir: Path
) -> None:
    workspace, base = _repo(tmp_path, "transcript")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base))

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id, run_index)
        return ScriptedModel(
            [
                _activation_message(),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        )

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    report = run_experiment(protocol, deps)

    assert report.records[0]["status"] == "completed"
    records = _transcript_records(deps.out_dir, 1)
    assert [r["kind"] for r in records] == ["request", "response"] * 4
    assert [r["seq"] for r in records] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert "activate_implement" in records[0]["tools"]
    assert records[0]["new_messages"]
    assert isinstance(records[1]["message"], dict)
    check = report.records[0]["oracle_access_check"]
    assert check["checked"] is True
    assert check["matches"] == []
    assert check["transcript_digest"] == hashlib.sha256(
        (deps.out_dir / "evidence" / "run-001" / "transcript.jsonl").read_bytes()
    ).hexdigest()


def _voided_run(
    tmp_path: Path, skills_dir: Path, tool_args: dict[str, object]
) -> tuple[ExperimentDeps, dict[str, Any]]:
    workspace, base = _repo(tmp_path, "void")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base))

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id, run_index)
        return ScriptedModel(
            [
                _activation_message(),
                _tool_message("test_targeted", tool_args, "call_sig"),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        )

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    report = run_experiment(protocol, deps)
    return deps, report.records[0]


def test_repo_url_voids_run(tmp_path: Path, skills_dir: Path) -> None:
    _, record = _voided_run(
        tmp_path, skills_dir, {"path": "https://github.com/lbacik/coding-agent"}
    )
    assert record["status"] == STATUS_VOIDED
    assert REPO_SIGNAL in [
        m["signal"] for m in record["oracle_access_check"]["matches"]
    ]


def test_repo_dot_git_voids_run(tmp_path: Path, skills_dir: Path) -> None:
    _, record = _voided_run(
        tmp_path, skills_dir, {"path": "https://github.com/lbacik/coding-agent.git"}
    )
    assert record["status"] == STATUS_VOIDED
    assert record["oracle_access_check"]["matches"][0]["signal"] == REPO_SIGNAL


def test_fixtures_path_voids_run(tmp_path: Path, skills_dir: Path) -> None:
    _, record = _voided_run(
        tmp_path, skills_dir, {"path": "fixtures/exp-69/oracles/f1_silent_stop.py"}
    )
    assert record["status"] == STATUS_VOIDED
    signals = [m["signal"] for m in record["oracle_access_check"]["matches"]]
    assert FIXTURES_SIGNAL in signals


def test_oracle_filename_voids_run(tmp_path: Path, skills_dir: Path) -> None:
    _, record = _voided_run(tmp_path, skills_dir, {"path": "oracle_probe.txt"})
    assert record["status"] == STATUS_VOIDED
    assert "oracle_probe.txt" in [
        m["signal"] for m in record["oracle_access_check"]["matches"]
    ]


def test_sandbox_only_stays_completed(tmp_path: Path, skills_dir: Path) -> None:
    _, record = _voided_run(
        tmp_path,
        skills_dir,
        {"path": "https://github.com/lbacik/coding-agent-sandbox"},
    )
    assert record["status"] == "completed"
    assert record["oracle_access_check"]["matches"] == []


def test_runtime_supplied_text_does_not_match(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    opening = "clone https://github.com/lbacik/coding-agent for context"
    path.write_text(
        json.dumps(
            {
                "seq": 1,
                "kind": "request",
                "tools": ["probe"],
                "message_count": 1,
                "new_messages": [{"type": "human", "content": opening}],
            }
        )
        + "\n"
        + json.dumps(
            {
                "seq": 2,
                "kind": "response",
                "message": {"type": "ai", "content": "ok", "tool_calls": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert scan_transcript(path, [REPO_SIGNAL]) == []


def test_tool_result_only_matches(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "seq": 1,
                "kind": "request",
                "tools": ["probe"],
                "message_count": 1,
                "new_messages": [{"type": "human", "content": "brief"}],
            }
        )
        + "\n"
        + json.dumps(
            {
                "seq": 2,
                "kind": "response",
                "message": {
                    "type": "ai",
                    "content": "calling",
                    "tool_calls": [
                        {"name": "probe", "id": "c1", "args": {"path": "clean"}}
                    ],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "seq": 3,
                "kind": "request",
                "tools": ["probe"],
                "message_count": 3,
                "new_messages": [
                    {
                        "type": "tool",
                        "content": "see fixtures/exp-69/oracles/x.py",
                        "tool_call_id": "c1",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    matches = scan_transcript(path, [FIXTURES_SIGNAL])
    assert len(matches) == 1
    assert matches[0]["kind"] == "tool_result"


def test_reviewer_conversation_matches(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        {
            "seq": 1,
            "kind": "request",
            "tools": ["a"],
            "message_count": 1,
            "new_messages": [{"type": "human", "content": "brief"}],
        },
        {
            "seq": 2,
            "kind": "response",
            "message": {"type": "ai", "content": "ok", "tool_calls": []},
        },
        {
            "seq": 3,
            "kind": "request",
            "tools": ["b"],
            "message_count": 1,
            "new_messages": [{"type": "human", "content": "review brief"}],
        },
        {
            "seq": 4,
            "kind": "response",
            "message": {
                "type": "ai",
                "content": "calling",
                "tool_calls": [
                    {
                        "name": "probe",
                        "id": "c9",
                        "args": {"cmd": "git clone https://github.com/lbacik/coding-agent"},
                    }
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    matches = scan_transcript(path, [REPO_SIGNAL])
    assert len(matches) == 1
    assert matches[0]["conversation"] == 2
    assert matches[0]["seq"] == 4


def test_missing_transcript_voids_with_unchecked(tmp_path: Path) -> None:
    check = build_oracle_access_check(tmp_path / "nope.jsonl", [REPO_SIGNAL])
    assert check == {"checked": False, "transcript_digest": None, "matches": []}


def test_harness_missing_transcript_voids(tmp_path: Path) -> None:
    from coding_agent.experiment.harness import _apply_oracle_access_check

    record: dict[str, Any] = {
        "status": "completed",
        "oracle_access_check": {"checked": False, "transcript_digest": None, "matches": []},
    }
    _apply_oracle_access_check(record, tmp_path / "nope.jsonl", [REPO_SIGNAL])
    assert record["status"] == STATUS_VOIDED
    assert record["oracle_access_check"] == {
        "checked": False,
        "transcript_digest": None,
        "matches": [],
    }


def test_harness_unreadable_transcript_voids(tmp_path: Path, skills_dir: Path) -> None:
    workspace, base = _repo(tmp_path, "corrupt")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base))
    out_dir = tmp_path / "experiment-out"

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id, run_index)

        class Corrupting(ScriptedModel):
            def invoke(self, input: object) -> BaseMessage:
                target = out_dir / "evidence" / "run-001" / "transcript.jsonl"
                target.write_text("not json\n", encoding="utf-8")
                return _silent_message()

            def bind_tools(
                self,
                tools: object,
                tool_choice: object = None,
            ) -> ScriptedModel:
                return self

        return Corrupting([])

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    report = run_experiment(protocol, deps)
    assert report.records[0]["status"] == STATUS_VOIDED
    assert report.records[0]["oracle_access_check"]["checked"] is False


def test_voided_run_keeps_oracle_and_spend_but_not_score(
    tmp_path: Path, skills_dir: Path
) -> None:
    workspace, base = _repo(tmp_path, "mixed")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base, runs=2))

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id)
        if run_index == 1:
            return ScriptedModel([_activation_message(), _silent_message()])
        return ScriptedModel(
            [
                _activation_message(),
                _tool_message(
                    "test_targeted",
                    {"path": "https://github.com/lbacik/coding-agent"},
                    "call_sig",
                ),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        )

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    report = run_experiment(protocol, deps)

    assert [r["status"] for r in report.records] == ["completed", STATUS_VOIDED]
    voided = report.records[1]
    assert voided["result"]["oracle"]["exit_code"] == 0
    assert voided["meters"]["model_calls"] is not None
    aggregate = build_aggregate(protocol, report.records)
    assert aggregate["cells"][0]["runs"] == 1
    assert aggregate["cells"][0]["voided"] == 1
    summary = json.loads((deps.out_dir / "report.json").read_text(encoding="utf-8"))
    assert len(summary["voided"]) == 1
    assert len(report.records) == 2


def test_transcript_redacts_registered_secrets(
    tmp_path: Path, skills_dir: Path
) -> None:
    workspace, base = _repo(tmp_path, "redact")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base))

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id, run_index)
        return ScriptedModel(
            [
                _activation_message(),
                _tool_message(
                    "test_targeted", {"path": "uses token secret-token-123"}, "call_1"
                ),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        )

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    deps.redact = {"GITHUB_TOKEN": "secret-token-123"}
    run_experiment(protocol, deps)
    text = (deps.out_dir / "evidence" / "run-001" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert "secret-token-123" not in text
    assert "«redacted:GITHUB_TOKEN»" in text


def test_refused_runs_carry_an_unchecked_block(
    tmp_path: Path, skills_dir: Path
) -> None:
    workspace, base = _repo(tmp_path, "refused")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base)
    document["per_attempt_cost_usd"] = 5.0
    document["overall_cap_usd"] = 4.0
    protocol = parse_protocol(document)

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        raise AssertionError("no Attempt may start past the cap")

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    report = run_experiment(protocol, deps)
    assert report.records[0]["status"] == "cap-refused"
    assert report.records[0]["oracle_access_check"] == {
        "checked": False,
        "transcript_digest": None,
        "matches": [],
    }


def test_signals_come_from_protocol_oracle_files() -> None:
    digest = "a" * 64
    base = "b" * 40
    protocol = parse_protocol(
        {
            "schema_version": 1,
            "arms": [{"id": "contract"}],
            "fixtures": [
                {
                    "id": "fA",
                    "issue_number": 7,
                    "issue_body_digest": digest,
                    "base_revision": base,
                    "oracle": ["cat", "x"],
                    "oracle_files": {"_oracle/f1_silent_stop.py": "x\n"},
                }
            ],
            "runs_per_cell": 1,
            "order": [{"arm": "contract", "fixture": "fA", "run": 1}],
            "per_attempt_cost_usd": 5.0,
            "overall_cap_usd": 100.0,
            "pinned_model": dict(PINNED_MODEL),
        }
    )
    signals = oracle_access_signals(protocol)
    assert REPO_SIGNAL in signals
    assert FIXTURES_SIGNAL in signals
    assert "f1_silent_stop.py" in signals


def test_recheck_script_reproduces(tmp_path: Path, skills_dir: Path) -> None:
    workspace, base = _repo(tmp_path, "recheck")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base, runs=2))

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        _ = (arm_id, fixture_id)
        if run_index == 1:
            return ScriptedModel([_activation_message(), _silent_message()])
        return ScriptedModel(
            [
                _activation_message(),
                _tool_message(
                    "test_targeted", {"path": "fixtures/exp-69/x"}, "call_sig"
                ),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        )

    deps = _deps(tmp_path, workspace, skills_dir, make_model)
    run_experiment(protocol, deps)
    script = Path(__file__).resolve().parent.parent / "scripts" / "exp-69-oracle-access-check.py"
    completed = subprocess.run(
        [sys.executable, str(script), str(deps.out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "checked=True" in completed.stdout
