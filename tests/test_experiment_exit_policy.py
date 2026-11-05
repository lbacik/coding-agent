"""The experiment-only "silence ends the stage" exit policy (ADR 0019).

The step subgraph takes an exit policy value whose default is the contract
behaviour (contract §8, ADR 0014): ``report_completion`` is the only
non-exceptional exit and a tool-less response draws up to two deterministic
silence continuations per work node. The control arm ends the implementation
stage on the first tool-less response, issues no continuation, and leaves
``report_completion`` off the model's tool surface. Everything else —
explicit activation, every other tool and its schema, ceilings and Gates —
stays identical, and the control policy is unreachable from production: no
Workflow Configuration field, CLI flag or environment variable selects it;
only the experiment harness constructs it.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

from langchain_core.messages import AIMessage

from coding_agent.experiment.harness import RecordingModel
from coding_agent.experiment.protocol import KNOWN_EXIT_POLICIES
from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.exit_policy import (
    DEFAULT_STEP_EXIT_POLICY,
    SILENCE_ENDS_STAGE_EXIT_POLICY,
    StepExitPolicy,
)
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.silence import (
    MAX_SILENCE_CONTINUATIONS,
    SILENCE_CONTINUATION_MARKER,
    is_silence_continuation,
)
from coding_agent.tracker.port import TrackedIssue
from conftest import init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

EFFECTIVE_CONFIGURATION = {
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


def _activation_message(call_id: str = "call_activate_1") -> AIMessage:
    return AIMessage(
        content="activating",
        tool_calls=[
            {"name": "activate_implement", "args": {}, "id": call_id, "type": "tool_call"}
        ],
    )


def _silent_message(text: str = "still thinking") -> AIMessage:
    return AIMessage(content=text, tool_calls=[])


def _make_workspace(tmp_path: Path, name: str) -> Path:
    src = tmp_path / name
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "add project profile"], src)
    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
    )
    workdir = tmp_path / f"{name}-work"
    run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmp_path)
    return workdir


def _run_with_recording(
    tmp_path: Path,
    skills_dir: Path,
    script: list[AIMessage],
    policy: StepExitPolicy,
) -> tuple[ScriptedModel, RecordingModel, str, str]:
    """Run one scripted Attempt under ``policy``, returning its recording."""
    workspace = _make_workspace(tmp_path, f"policy-{policy.name}")
    ledger = RunLedger(tmp_path / f"{policy.name}.sqlite3")
    try:
        inner = ScriptedModel(script)
        recording = RecordingModel(inner)
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=FakeTracker(),
            ledger=ledger,
            model=recording,
            workspace=workspace,
            effective_configuration=dict(EFFECTIVE_CONFIGURATION),
            skills_dir=skills_dir,
            exit_policy=policy,
        )
        return inner, recording, result.outcome, result.reason
    finally:
        ledger.close()


def _continuation_count(scripted: ScriptedModel) -> int:
    """Continuations in the final invocation (history is cumulative)."""
    assert scripted.invocations, "the model was never called"
    return sum(
        1 for message in scripted.invocations[-1] if is_silence_continuation(message)
    )


def _invocation_messages(scripted: ScriptedModel) -> list[object]:
    messages: list[AIMessage | object] = []
    for invocation in scripted.invocations:
        messages.extend(invocation)
    return messages


def _schema_by_name(recording: RecordingModel) -> dict[str, str]:
    """The canonical tool schema per name across every offered surface."""
    schemas: dict[str, str] = {}
    for _, tools in recording.log.surfaces:
        for tool in tools:
            try:
                args_schema = dict(tool.args)
            except Exception:
                args_schema = {"repr": repr(tool.args)}
            canonical = json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "args": json.loads(
                        json.dumps(args_schema, sort_keys=True, default=str)
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if tool.name in schemas:
                assert schemas[tool.name] == canonical, (
                    f"tool {tool.name!r} schema varies between binds"
                )
            schemas[tool.name] = canonical
    return schemas


def test_control_first_silence_ends_stage_with_no_continuation(
    tmp_path: Path, skills_dir: Path
) -> None:
    """Under the control arm the first tool-less response exits the stage."""
    script = [_activation_message(), _silent_message()]
    scripted, recording, outcome, reason = _run_with_recording(
        tmp_path, skills_dir, script, SILENCE_ENDS_STAGE_EXIT_POLICY
    )
    assert outcome == "failed"
    assert reason == "undeclared"
    # Activation plus the single silent response: no continuation was ever
    # issued, so the model was never called again.
    assert recording.log.invocations == 2
    assert _continuation_count(scripted) == 0
    for message in _invocation_messages(scripted):
        content = getattr(message, "content", "")
        assert SILENCE_CONTINUATION_MARKER not in str(content)


def test_contract_still_answers_silence_twice(
    tmp_path: Path, skills_dir: Path
) -> None:
    """The default arm is unchanged: silence draws two continuations."""
    script = [
        _activation_message(),
        _silent_message("quiet one"),
        _silent_message("quiet two"),
        _silent_message("quiet three"),
    ]
    scripted, recording, outcome, reason = _run_with_recording(
        tmp_path, skills_dir, script, DEFAULT_STEP_EXIT_POLICY
    )
    assert outcome == "failed"
    assert reason == "undeclared"
    assert recording.log.invocations == 4
    assert _continuation_count(scripted) == MAX_SILENCE_CONTINUATIONS == 2


def test_control_hides_report_completion_other_schemas_byte_equal(
    tmp_path: Path, skills_dir: Path
) -> None:
    """The only intended tool-surface difference is the absent exit tool."""
    _, control_recording, _, _ = _run_with_recording(
        tmp_path,
        skills_dir,
        [_activation_message(), _silent_message()],
        SILENCE_ENDS_STAGE_EXIT_POLICY,
    )
    _, contract_recording, _, _ = _run_with_recording(
        tmp_path,
        skills_dir,
        [
            _activation_message(),
            _silent_message("quiet one"),
            _silent_message("quiet two"),
            _silent_message("quiet three"),
        ],
        DEFAULT_STEP_EXIT_POLICY,
    )
    control_schemas = _schema_by_name(control_recording)
    contract_schemas = _schema_by_name(contract_recording)
    assert "report_completion" not in control_schemas
    assert "report_completion" in contract_schemas
    assert set(control_schemas) == set(contract_schemas) - {"report_completion"}
    for name in control_schemas:
        assert control_schemas[name] == contract_schemas[name], (
            f"tool {name!r} schema differs between arms"
        )


def _repo_files_containing(token: str) -> list[str]:
    root = Path(__file__).resolve().parent.parent / "src" / "coding_agent"
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if token in path.read_text(encoding="utf-8"):
            hits.append(path.relative_to(root).as_posix())
    return hits


def test_control_policy_unreachable_from_production() -> None:
    """No production path names or constructs the control exit policy."""
    # The seam's parameter name lives only in the step subgraph (which takes
    # the policy value) and the experiment package (which constructs it).
    assert _repo_files_containing("exit_policy") == [
        "experiment/harness.py",
        "experiment/protocol.py",
        "runtime/graph/attempt.py",
        "runtime/graph/context.py",
        "runtime/graph/step.py",
    ]
    # No hyphenated twin exists anywhere: no CLI flag names the policy.
    assert _repo_files_containing("exit-policy") == []
    assert _repo_files_containing("silence_ends_stage") == [
        "experiment/__init__.py",
        "experiment/harness.py",
        "experiment/protocol.py",
        "runtime/exit_policy.py",
    ]
    assert _repo_files_containing("SILENCE_ENDS") == [
        "experiment/harness.py",
        "runtime/exit_policy.py",
    ]
    # The runtime seam itself defaults to the contract behaviour.
    assert DEFAULT_STEP_EXIT_POLICY.name == "contract"
    assert SILENCE_ENDS_STAGE_EXIT_POLICY.name == "silence_ends_stage"
    assert "silence_ends_stage" in KNOWN_EXIT_POLICIES
    assert AttemptContext.__dataclass_fields__["exit_policy"].default.name == (
        "contract"
    )
    signature = inspect.signature(run_attempt)
    assert signature.parameters["exit_policy"].default == DEFAULT_STEP_EXIT_POLICY
    # The production entry path passes no exit policy at all.
    supervisor = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "coding_agent"
        / "worker"
        / "supervisor.py"
    ).read_text(encoding="utf-8")
    assert "exit_policy" not in supervisor
    assert "SILENCE_ENDS" not in supervisor
    assert "silence_ends_stage" not in supervisor


def test_tracker_issue_body_digest_helpers() -> None:
    """The protocol pins the Target Issue body its digest covers."""
    from coding_agent.experiment.protocol import issue_body_digest

    issue = TrackedIssue(number=7, title="t", body="hello\n")
    assert issue_body_digest(issue.body) == issue_body_digest("hello\n")
    assert len(issue_body_digest(issue.body)) == 64
