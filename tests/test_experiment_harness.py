"""The experiment harness: interleaved Attempts with per-run evidence (ADR 0019).

Driven by a validated protocol file (arms, fixtures, runs per cell, the
declared interleaved run order, the per-Attempt cost limit, the overall cap,
an oracle per fixture). The harness runs the Attempts sequentially in order,
refuses runs that would break identical-state preconditions or the overall
cap, runs each oracle after its Attempt ends against the captured tree
(oracle files never enter the workspace during the Attempt), and writes one
JSON record per run plus the aggregate. Missing measurements are recorded as
missing, never as zero.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.experiment.harness import (
    ExperimentDeps,
    build_aggregate,
    median,
    run_experiment,
    spread,
    tree_diff,
)
from coding_agent.experiment.protocol import (
    ExperimentProtocol,
    ProtocolError,
    issue_body_digest,
    load_protocol,
    parse_protocol,
)
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


def _test_targeted_message(call_id: str = "call_test_1") -> AIMessage:
    return AIMessage(
        content="running the existing suite",
        tool_calls=[
            {
                "name": "test_targeted",
                "args": {"path": "tests/test_greet.py"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _two_commit_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo with two commits; returns (source, base_a, base_b)."""
    src = tmp_path / "origin-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
    (src / "greeting.txt").write_text("hello\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "base A"], src)
    base_a = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    (src / "greeting.txt").write_text("hello again\n", encoding="utf-8")
    run_git(["commit", "-q", "-am", "base B"], src)
    base_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    return src, base_a, base_b


def _clone(src: Path, dest: Path) -> Path:
    run_git(["clone", "-q", str(src), str(dest)], cwd=src.parent)
    return dest


class _OracleAbsenceCheckingModel:
    """A scripted model that proves oracle files stay out of the workspace.

    Wraps a :class:`ScriptedModel` and asserts, on every model call the
    Attempt makes, that none of the fixture's oracle files exists in the
    workspace yet. ``notes`` records each checked call, so the test can
    prove the check actually ran.
    """

    def __init__(
        self,
        scripted: ScriptedModel,
        workspace: Path,
        oracle_names: tuple[str, ...],
        notes: list[bool],
    ) -> None:
        self._scripted = scripted
        self._workspace = workspace
        self._oracle_names = oracle_names
        self._notes = notes

    def bind_tools(
        self,
        tools: Any,
        tool_choice: str | None = None,
    ) -> InvokableToolModel:
        self._scripted.bind_tools(tools, tool_choice)
        return self

    def invoke(self, input: Any) -> BaseMessage:
        for name in self._oracle_names:
            assert not (self._workspace / name).exists(), (
                f"oracle file {name!r} present in the workspace during the Attempt"
            )
        self._notes.append(True)
        return self._scripted.invoke(input)


def _make_model_factory(
    workspace: Path, oracle_names: tuple[str, ...], notes: list[bool]
) -> Callable[[str, str, int], InvokableToolModel]:
    def make_model(
        arm_id: str, fixture_id: str, run_index: int
    ) -> InvokableToolModel:
        _ = (fixture_id, run_index)
        if arm_id == "control":
            script = [_activation_message(), _silent_message()]
        else:
            script = [
                _activation_message(),
                _test_targeted_message(),
                _silent_message("quiet one"),
                _silent_message("quiet two"),
                _silent_message("quiet three"),
            ]
        inner = ScriptedModel(script)
        return _OracleAbsenceCheckingModel(inner, workspace, oracle_names, notes)

    return make_model


def _protocol_document(
    digest: str, base_a: str, base_b: str
) -> dict[str, Any]:
    order: list[dict[str, Any]] = []
    for run in (1, 2):
        for arm in ("contract", "control"):
            for fixture in ("fA", "fB"):
                order.append({"arm": arm, "fixture": fixture, "run": run})
    # Interleave across arms: contract/fA, control/fA, contract/fB, ...
    order.sort(key=lambda entry: (entry["run"], entry["fixture"], entry["arm"]))
    return {
        "schema_version": 1,
        "arms": [
            {"id": "contract", "exit_policy": "contract"},
            {"id": "control", "exit_policy": "silence_ends_stage"},
        ],
        "fixtures": [
            {
                "id": "fA",
                "issue_number": 7,
                "issue_body_digest": digest,
                "base_revision": base_a,
                "oracle": list(ORACLE_COMMAND),
                "oracle_files": dict(ORACLE_FILES),
            },
            {
                "id": "fB",
                "issue_number": 8,
                "issue_body_digest": digest,
                "base_revision": base_b,
                "oracle": list(ORACLE_COMMAND),
                "oracle_files": dict(ORACLE_FILES),
            },
        ],
        "runs_per_cell": 2,
        "order": order,
        "per_attempt_cost_usd": 5.0,
        "overall_cap_usd": 1000.0,
        "pinned_model": dict(PINNED_MODEL),
    }


def _deps(
    tmp_path: Path,
    workspace: Path,
    skills_dir: Path,
    notes: list[bool],
    make_model: Callable[[str, str, int], InvokableToolModel] | None = None,
) -> ExperimentDeps:
    out_dir = tmp_path / "experiment-out"
    factory = make_model
    if factory is None:
        factory = _make_model_factory(workspace, tuple(ORACLE_FILES), notes)
    return ExperimentDeps(
        tracker=FakeTracker(),
        workspace=workspace,
        skills_dir=skills_dir,
        out_dir=out_dir,
        effective_configuration=dict(EFFECTIVE_CONFIGURATION),
        make_model=factory,
        runtime_commit="test-commit",
        skill_bundle_digest="test-skills-digest",
        oracle_timeout_seconds=60.0,
    )


def _number_or_none(value: object) -> float | None:
    if value is None:
        return None
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def test_full_protocol_runs_in_declared_order_with_complete_records(
    tmp_path: Path, skills_dir: Path
) -> None:
    """2 arms x 2 fixtures x 2 runs: order, pins, behaviour, meters, result."""
    src, base_a, base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace")
    digest = issue_body_digest(FakeTracker().issue.body)
    protocol = parse_protocol(_protocol_document(digest, base_a, base_b))
    notes: list[bool] = []
    deps = _deps(tmp_path, workspace, skills_dir, notes)

    report = run_experiment(protocol, deps)

    assert report.halted is None
    assert len(report.records) == 8
    expected = [(entry.arm, entry.fixture, entry.run) for entry in protocol.order]
    assert [
        (record["pins"]["arm"], record["pins"]["fixture"], record["pins"]["run_index"])
        for record in report.records
    ] == expected
    assert [record["seq"] for record in report.records] == [1, 2, 3, 4, 5, 6, 7, 8]
    # The oracle-absence check ran on every Attempt's model calls.
    assert len(notes) > 8
    # Per-run JSON documents plus the aggregate and the summary.
    runs_dir = deps.out_dir / "runs"
    assert (deps.out_dir / "aggregate.json").is_file()
    assert (deps.out_dir / "report.json").is_file()
    assert len(list(runs_dir.glob("run-*.json"))) == 8
    digests: dict[str, str] = {}
    for record in report.records:
        pins = record["pins"]
        assert pins["runtime_commit"] == "test-commit"
        assert pins["skill_bundle_digest"] == "test-skills-digest"
        assert pins["pinned_model"] == {
            "provider": "anthropic",
            "model": "test-model",
            "effort": "high",
        }
        assert pins["per_attempt_cost_usd"] == 5.0
        tool_digest = pins["tool_schema_digest"]
        assert isinstance(tool_digest, str) and len(tool_digest) == 64
        digests.setdefault(pins["arm"], tool_digest)
        assert record["status"] == "completed"
        preconditions = record["preconditions"]
        assert preconditions["issue_body_match"] is True
        assert preconditions["selection_label_present"] is True
        assert preconditions["base_revision_match"] is True
        assert preconditions["oracle_files_absent"] is True
        assert isinstance(preconditions["leftover_branches_removed"], list)
        behaviour = record["behaviour"]
        assert behaviour["activation_trace"] == ["activate_implement"]
        assert behaviour["claim"] is None
        assert behaviour["claim_calls"] == 0
        assert behaviour["claim_force_accepted"] is False
        assert behaviour["terminal_outcome"] == "failed"
        assert behaviour["terminal_reason"] == "undeclared"
        assert behaviour["implementation_outcome"] == "completion not established"
        assert behaviour["implementation_reason"] == "undeclared"
        assert isinstance(behaviour["criterion_evidence"], list)
        review = behaviour["review"]
        assert review["ran"] is False
        assert isinstance(review["status"], str)
        assert isinstance(review["provenance"], str)
        meters = record["meters"]
        assert isinstance(meters["model_calls"], int)
        for key in (
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "elapsed_seconds",
        ):
            _number_or_none(meters[key])
        result = record["result"]
        assert isinstance(result["working_tree_digest"], str)
        assert len(result["working_tree_digest"]) == 64
        assert isinstance(result["working_tree_diff"], str)
        assert result["working_tree_diff_empty"] is True
        oracle = result["oracle"]
        assert oracle["command"] == list(ORACLE_COMMAND)
        assert oracle["exit_code"] == 0
        assert "probe-ok" in str(oracle["stdout"])
        assert oracle["timed_out"] is False
        assert record["spend"]["spend_after_usd"] >= record["spend"]["spend_before_usd"]
    # The arms ran under different tool surfaces: the control digest drops
    # report_completion.
    assert digests["contract"] != digests["control"]
    # The control arm ends the stage on the first silence: fewer model calls.
    control_calls = [
        record["meters"]["model_calls"]
        for record in report.records
        if record["pins"]["arm"] == "control"
    ]
    contract_calls = [
        record["meters"]["model_calls"]
        for record in report.records
        if record["pins"]["arm"] == "contract"
    ]
    assert control_calls == [2, 2, 2, 2]
    assert contract_calls == [5, 5, 5, 5]
    # Only the contract arm ran the targeted test, so only it has evidence.
    for record in report.records:
        evidence = record["behaviour"]["criterion_evidence"]
        if record["pins"]["arm"] == "contract":
            assert len(evidence) == 1
            assert evidence[0]["passed"] is True
            assert isinstance(evidence[0]["test_id"], str)
        else:
            assert evidence == []
    # The oracle files reached the snapshots but never the workspace.
    assert not (workspace / "oracle_probe.txt").exists()
    snapshot_probe = deps.out_dir / "snapshots" / "run-001" / "oracle_probe.txt"
    assert snapshot_probe.read_text(encoding="utf-8") == "probe-ok\n"
    # The aggregate covers every arm x fixture cell with values and spread.
    aggregate = report.aggregate
    assert len(aggregate["cells"]) == 4
    for cell in aggregate["cells"]:
        assert cell["runs"] == 2
        assert len(cell["terminal"]) == 2
        for key, summary in cell["meters"].items():
            assert set(summary) == {"values", "missing", "median", "spread"}
            assert summary["missing"] == 0 or key != "model_calls"
            if key == "model_calls":
                assert summary["values"] == sorted(summary["values"])
                assert summary["median"] == median(summary["values"])
                assert summary["spread"] == spread(summary["values"])


def test_cap_refusal_stops_before_any_attempt(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A run whose limit would exceed the overall cap is never started."""
    src, base_a, base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace-cap")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base_a, base_b)
    document["per_attempt_cost_usd"] = 5.0
    document["overall_cap_usd"] = 4.0
    protocol = parse_protocol(document)
    calls: list[tuple[str, str, int]] = []

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        calls.append((arm_id, fixture_id, run_index))
        raise AssertionError("no Attempt may start past the cap")

    deps = _deps(tmp_path, workspace, skills_dir, [], make_model=make_model)
    report = run_experiment(protocol, deps)

    assert calls == []
    assert len(report.records) == 1
    record = report.records[0]
    assert record["status"] == "cap-refused"
    assert "overall cap" in str(record["refusal_reason"])
    assert report.halted is not None
    assert report.halted["seq"] == 1
    assert report.spend_total_usd == 0.0


def test_state_refusal_on_issue_body_mismatch(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A Target Issue body that misses its digest refuses the run."""
    src, base_a, base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace-state")
    document = _protocol_document("0" * 64, base_a, base_b)
    protocol = parse_protocol(document)
    calls: list[tuple[str, str, int]] = []

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        calls.append((arm_id, fixture_id, run_index))
        raise AssertionError("no Attempt may start on a mismatched state")

    deps = _deps(tmp_path, workspace, skills_dir, [], make_model=make_model)
    report = run_experiment(protocol, deps)

    assert calls == []
    assert len(report.records) == 8
    assert all(
        record["status"] == "precondition-refused" for record in report.records
    )
    assert "digest" in str(report.records[0]["refusal_reason"])
    assert report.records[0]["preconditions"]["issue_body_match"] is False


def test_state_refusal_on_base_revision_mismatch(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A Base Revision that will not check out refuses the run."""
    src, base_a, _base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace-rev")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base_a, base_b="0" * 40)
    document["runs_per_cell"] = 1
    document["order"] = document["order"][:4]
    protocol = parse_protocol(document)
    # The default factory runs the fA Attempts; the fB runs must refuse
    # in preconditions, before any model is built.
    deps = _deps(tmp_path, workspace, skills_dir, [])
    report = run_experiment(protocol, deps)

    refused = [record for record in report.records if record["status"] != "completed"]
    assert refused, "the fB fixture must refuse every run"
    assert all(
        record["status"] == "precondition-refused" for record in refused
    )
    assert all(
        record["pins"]["fixture"] == "fB" for record in refused
    )
    assert "Base Revision" in str(refused[0]["refusal_reason"])


def test_attempt_error_records_missing_meters_not_zero(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A measurement the runtime never reports stays explicitly missing."""
    src, base_a, _base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace-error")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base_a, base_a)
    document["fixtures"] = [document["fixtures"][0]]
    document["runs_per_cell"] = 1
    document["order"] = [
        {"arm": "contract", "fixture": "fA", "run": 1},
        {"arm": "control", "fixture": "fA", "run": 1},
    ]
    protocol = parse_protocol(document)

    def make_model(arm_id: str, fixture_id: str, run_index: int) -> InvokableToolModel:
        raise RuntimeError("boom before any measurement")

    deps = _deps(tmp_path, workspace, skills_dir, [], make_model=make_model)
    report = run_experiment(protocol, deps)

    assert len(report.records) == 2
    for record in report.records:
        assert record["status"] == "attempt-error"
        for key, value in record["meters"].items():
            assert value is None, f"meter {key!r} must be missing, not {value!r}"


def test_oracle_runs_after_the_attempt_outside_the_workspace(
    tmp_path: Path, skills_dir: Path
) -> None:
    """The oracle sees the final tree; its files never enter the workspace."""
    src, base_a, _base_b = _two_commit_repo(tmp_path)
    workspace = _clone(src, tmp_path / "workspace-oracle")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base_a, base_a)
    document["arms"] = [{"id": "control", "exit_policy": "silence_ends_stage"}]
    document["fixtures"] = [document["fixtures"][0]]
    document["runs_per_cell"] = 1
    document["order"] = [{"arm": "control", "fixture": "fA", "run": 1}]
    protocol = parse_protocol(document)
    notes: list[bool] = []
    deps = _deps(tmp_path, workspace, skills_dir, notes)

    report = run_experiment(protocol, deps)

    assert notes, "the Attempt must have called the model"
    assert len(report.records) == 1
    oracle = report.records[0]["result"]["oracle"]
    assert oracle["exit_code"] == 0
    assert "probe-ok" in str(oracle["stdout"])
    assert not (workspace / "oracle_probe.txt").exists()


def test_protocol_validation_rejects_bad_documents() -> None:
    """The harness validates the protocol before any paid call."""
    import copy

    digest = "a" * 64
    base = "b" * 40
    document = _protocol_document(digest, base, base)
    assert isinstance(parse_protocol(document), ExperimentProtocol)

    def broken(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        candidate = copy.deepcopy(document)
        mutate(candidate)
        return candidate

    bad_schema = broken(lambda doc: doc.update(schema_version=999))
    bad_arms = broken(lambda doc: doc.update(arms=[]))
    dup_arms = broken(
        lambda doc: doc.update(arms=[{"id": "contract"}, {"id": "contract"}])
    )
    unknown_arm = broken(lambda doc: doc["order"][0].update(arm="nope"))
    short_order = broken(lambda doc: doc.update(order=doc["order"][:-1]))
    bad_cap = broken(lambda doc: doc.update(overall_cap_usd=0.0))
    bad_digest = broken(
        lambda doc: doc["fixtures"][0].update(issue_body_digest="zz")
    )
    for bad in (
        bad_schema,
        bad_arms,
        dup_arms,
        unknown_arm,
        short_order,
        bad_cap,
        bad_digest,
    ):
        with pytest.raises(ProtocolError):
            parse_protocol(bad)


def test_protocol_yaml_round_trip(tmp_path: Path) -> None:
    """YAML protocol files load exactly like the decoded document."""
    digest = "c" * 64
    base = "d" * 40
    path = tmp_path / "protocol.yaml"
    path.write_text(yaml.safe_dump(_protocol_document(digest, base, base)), encoding="utf-8")
    protocol = load_protocol(path)
    assert protocol.runs_per_cell == 2
    assert protocol.arm_ids() == ("contract", "control")
    assert protocol.fixture_ids() == ("fA", "fB")
    assert protocol.pinned_model.provider == "anthropic"
    assert protocol.overall_cap_usd == 1000.0


def test_median_and_spread() -> None:
    assert median([]) is None
    assert spread([]) is None
    assert median([3.0]) == 3.0
    assert median([1.0, 2.0, 3.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert spread([1.0, 4.0]) == 3.0
    protocol = parse_protocol(_protocol_document("e" * 64, "f" * 40, "f" * 40))
    assert len(build_aggregate(protocol, [])["cells"]) == 4


def test_tree_diff_against_base_revision_survives_attempt_commit(
    tmp_path: Path,
) -> None:
    """``tree_diff`` sees committed Attempt work; moving ``HEAD`` is a no-op."""
    src = tmp_path / "diff-src"
    init_origin_repo(src)
    (src / "f.txt").write_text("hello\n", encoding="utf-8")
    (src / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "base"], src)
    base = run_git(["rev-parse", "HEAD"], src)
    workspace = _clone(src, tmp_path / "diff-workspace")

    (workspace / "f.txt").write_text("hello changed\n", encoding="utf-8")
    (workspace / "g.txt").write_text("new content\n", encoding="utf-8")
    venv_junk = workspace / ".venv" / "junk.txt"
    venv_junk.parent.mkdir(parents=True, exist_ok=True)
    venv_junk.write_text("bootstrap junk\n", encoding="utf-8")

    head_before = run_git(["rev-parse", "HEAD"], workspace)
    branch_before = run_git(["branch", "--show-current"], workspace)
    index_before = run_git(["diff", "--cached", "--name-only"], workspace)
    diff_text, is_empty = tree_diff(workspace, base)
    assert "hello changed" in diff_text
    assert "new content" in diff_text
    assert "g.txt" in diff_text
    assert "??" not in diff_text
    assert ".venv" not in diff_text
    assert is_empty is False
    assert run_git(["rev-parse", "HEAD"], workspace) == head_before
    assert run_git(["branch", "--show-current"], workspace) == branch_before
    assert run_git(["diff", "--cached", "--name-only"], workspace) == index_before

    # The Attempt-style commit moves ``HEAD``; the diff must not change.
    run_git(["add", "-A"], workspace)
    run_git(["commit", "-q", "-m", "delivery snapshot"], workspace)
    assert run_git(["rev-parse", "HEAD"], workspace) != head_before
    committed_text, committed_empty = tree_diff(workspace, base)
    assert committed_text == diff_text
    assert committed_empty is False

    # A tree back at the Base Revision reads as an empty diff.
    run_git(["reset", "-q", "--hard", base], workspace)
    run_git(["clean", "-q", "-fdx"], workspace)
    clean_text, clean_empty = tree_diff(workspace, base)
    assert clean_text == ""
    assert clean_empty is True


class _CommittingModel:
    """A scripted model that commits its work mid-Attempt, like the real
    Delivery Snapshot commit does before review and during delivery."""

    def __init__(self, scripted: ScriptedModel, workspace: Path) -> None:
        self._scripted = scripted
        self._workspace = workspace
        self._committed = False

    def bind_tools(
        self,
        tools: Any,
        tool_choice: str | None = None,
    ) -> InvokableToolModel:
        self._scripted.bind_tools(tools, tool_choice)
        return self

    def invoke(self, input: Any) -> BaseMessage:
        if not self._committed:
            self._committed = True
            (self._workspace / "greeting.txt").write_text(
                "hello from the attempt\n", encoding="utf-8"
            )
            (self._workspace / "new_feature.txt").write_text(
                "brand new content\n", encoding="utf-8"
            )
            venv_junk = self._workspace / ".venv" / "junk.txt"
            venv_junk.parent.mkdir(parents=True, exist_ok=True)
            venv_junk.write_text("bootstrap junk\n", encoding="utf-8")
            run_git(["add", "-A"], self._workspace)
            run_git(["commit", "-q", "-m", "delivery snapshot"], self._workspace)
        return self._scripted.invoke(input)


def test_harness_records_diff_against_base_revision_after_attempt_commit(
    tmp_path: Path, skills_dir: Path
) -> None:
    """A run that commits mid-Attempt records its diff against the base."""
    src = tmp_path / "commit-src"
    init_origin_repo(src)
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(HAPPY_PROFILE, encoding="utf-8")
    (src / "greeting.txt").write_text("hello\n", encoding="utf-8")
    (src / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    run_git(["add", "."], src)
    run_git(["commit", "-q", "-m", "base"], src)
    base = run_git(["rev-parse", "HEAD"], src)
    workspace = _clone(src, tmp_path / "workspace-commit")
    digest = issue_body_digest(FakeTracker().issue.body)
    document = _protocol_document(digest, base, base)
    document["arms"] = [{"id": "control", "exit_policy": "silence_ends_stage"}]
    document["fixtures"] = [document["fixtures"][0]]
    document["runs_per_cell"] = 2
    document["order"] = [
        {"arm": "control", "fixture": "fA", "run": 1},
        {"arm": "control", "fixture": "fA", "run": 2},
    ]
    protocol = parse_protocol(document)

    def make_model(
        arm_id: str, fixture_id: str, run_index: int
    ) -> InvokableToolModel:
        _ = (arm_id, fixture_id)
        script = [_activation_message(), _silent_message()]
        if run_index == 1:
            return _CommittingModel(ScriptedModel(script), workspace)
        return ScriptedModel(script)

    deps = _deps(tmp_path, workspace, skills_dir, [], make_model=make_model)
    report = run_experiment(protocol, deps)

    assert len(report.records) == 2
    first = report.records[0]["result"]
    assert first["working_tree_diff_empty"] is False
    assert "hello from the attempt" in str(first["working_tree_diff"])
    assert "brand new content" in str(first["working_tree_diff"])
    assert "new_feature.txt" in str(first["working_tree_diff"])
    assert ".venv" not in str(first["working_tree_diff"])
    assert "??" not in str(first["working_tree_diff"])
    second = report.records[1]["result"]
    assert second["working_tree_diff"] == ""
    assert second["working_tree_diff_empty"] is True
