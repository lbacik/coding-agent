from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.runtime.result_capping import FilesystemArtifactStore, InMemoryArtifactStore
from coding_agent.profile.schema import Evidence, ProjectProfile, Toolchain
from coding_agent.validate.diagnostics import TargetedTestAdapter
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.runner import ExecutedCommand


def _profile(command: str = "run tests --junit-xml={evidence_dir}/targeted.xml") -> ProjectProfile:
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="true",
        test_all="true",
        test_targeted=command,
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/all.xml",
            test_targeted="{evidence_dir}/targeted.xml",
        ),
        checks=(),
        services=(),
    )


class _Runner:
    def __init__(self, result: ExecutedCommand) -> None:
        self.result = result
        self.calls: list[tuple[str, Path]] = []
        self.report: str | None = None

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self.calls.append((command, cwd))
        if self.report is not None:
            report_path = Path(command.split("--junit-xml=")[1].split()[0])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(self.report, encoding="utf-8")
        return self.result


def _adapter(tmp_path: Path, runner: _Runner, *, limit: int = 80) -> TargetedTestAdapter:
    context = CommandContext(runner, tmp_path, tmp_path, workspace_root=tmp_path)
    return TargetedTestAdapter(
        _profile(),
        context,
        artifact_store=InMemoryArtifactStore(),
        inline_limit=limit,
        attempt_id="49/1",
    )



def test_a_passing_run_names_its_passed_ids_in_the_evidence_form(tmp_path: Path) -> None:
    """§9: the ids a claim must name are shown on a pass, as Criterion Evidence records them."""
    runner = _Runner(ExecutedCommand(command="run tests", exit_code=0, stdout="2 passed", stderr=""))
    runner.report = (
        '<testsuite><testcase classname="tests.test_pricing" name="test_a"/>'
        '<testcase classname="tests.test_pricing" name="test_b"/></testsuite>'
    )
    payload = json.loads(_adapter(tmp_path, runner, limit=4000).run("tests/test_pricing.py"))
    assert payload["junit"]["passed_identifiers"] == [
        "tests.test_pricing::test_a",
        "tests.test_pricing::test_b",
    ]
    assert payload["junit"]["failure_identifiers"] == []

def test_targeted_diagnostics_are_structured_redacted_and_invocation_scoped(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        ExecutedCommand(
            command="run tests",
            exit_code=1,
            stdout="safe output",
            stderr="password=super-secret\n" + ("diagnostic " * 40),
        )
    )
    runner.report = '<testsuite><testcase classname="ExampleTest" name="fails"><failure/></testcase></testsuite>'
    adapter = _adapter(tmp_path, runner)

    result = adapter.run("tests/example_test.py", redactions={"PASSWORD": "super-secret"})
    payload = json.loads(result)

    assert payload["invocation_id"] == "49/1/targeted/1"
    assert payload["requested_path"] == "tests/example_test.py"
    assert payload["classification"] == "assertion_failure"
    assert payload["junit"] == {
        "executed_test_count": 1,
        "failure_identifiers": ["ExampleTest::fails"],
        "passed_identifiers": [],
    }
    assert "super-secret" not in result
    assert "redacted:PASSWORD" in result
    assert payload["artifacts"]
    assert all(item["artifact_id"].startswith("49/1/targeted/1/") for item in payload["artifacts"])
    assert payload["artifacts"][0]["byte_size"] == payload["artifacts"][0]["available_range"]["end"]


def test_large_streams_keep_the_structured_payload_within_the_default_inline_cap(
    tmp_path: Path,
) -> None:
    runner = _Runner(ExecutedCommand("run tests", 1, "x" * 10_000, "y" * 10_000))
    result = _adapter(tmp_path, runner, limit=4_000).run("tests/example.py")

    payload = json.loads(result)
    assert len(result.encode("utf-8")) <= 4_000
    assert payload["stdout"]["truncated"] is True
    assert payload["stderr"]["truncated"] is True
    assert len(payload["artifacts"]) == 2


def test_targeted_diagnostics_persist_truncated_streams_under_a_nested_attempt_id(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = _Runner(ExecutedCommand("run tests", 1, "x" * 200, ""))
    context = CommandContext(runner, tmp_path, tmp_path, workspace_root=tmp_path)
    adapter = TargetedTestAdapter(
        _profile(),
        context,
        artifact_store=store,
        inline_limit=80,
        attempt_id="#55/1",
    )

    payload = json.loads(adapter.run("tests/example.py"))

    artifact_id = payload["artifacts"][0]["artifact_id"]
    assert artifact_id == "#55/1/targeted/1/stdout"
    assert store.read(artifact_id) == "x" * 200


def test_targeted_diagnostics_distinguish_missing_executable_from_missing_tests(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        ExecutedCommand(
            command="vendor/bin/phpunit tests/Missing.php",
            exit_code=127,
            stdout="",
            stderr="/bin/sh: vendor/bin/phpunit: not found",
        )
    )
    result = _adapter(tmp_path, runner).run("tests/Missing.php")

    assert json.loads(result)["classification"] == "infrastructure_failure"


def test_valid_junit_failure_wins_over_an_assertion_message_containing_not_found(
    tmp_path: Path,
) -> None:
    runner = _Runner(ExecutedCommand("run tests", 1, "", "expected value not found"))
    runner.report = '<testsuite><testcase classname="Example" name="fails"><failure/></testcase></testsuite>'

    result = json.loads(_adapter(tmp_path, runner).run("tests/example.py"))

    assert result["classification"] == "assertion_failure"


@pytest.mark.parametrize(
    ("report", "exit_status", "classification"),
    [
        (None, 0, "missing_evidence"),
        ("not junit", 1, "invalid_evidence"),
        ('<testsuite tests="0"></testsuite>', 0, "no_tests_executed"),
        ('<testsuite><testcase classname="Example" name="ok"/></testsuite>', 0, "passed"),
        (
            '<testsuite><testcase classname="Example" name="fails"><failure/></testcase></testsuite>',
            1,
            "assertion_failure",
        ),
    ],
)
def test_targeted_diagnostics_classify_current_evidence(
    tmp_path: Path, report: str | None, exit_status: int, classification: str
) -> None:
    runner = _Runner(ExecutedCommand("run tests", exit_status, "", ""))
    runner.report = report

    result = json.loads(_adapter(tmp_path, runner).run("tests/example.py"))

    assert result["classification"] == classification


def test_assertion_failure_can_run_again_only_after_a_workspace_edit(tmp_path: Path) -> None:
    runner = _Runner(ExecutedCommand("run tests", 1, "", ""))
    runner.report = '<testsuite><testcase classname="Example" name="fails"><failure/></testcase></testsuite>'
    adapter = _adapter(tmp_path, runner)

    first = json.loads(adapter.run("tests/example.py"))
    second = json.loads(adapter.run("tests/example.py"))
    (tmp_path / "source.py").write_text("fixed", encoding="utf-8")
    third = json.loads(adapter.run("tests/example.py"))

    assert first["suppressed"] is False
    assert first["signature"] == second["signature"]
    assert second["suppressed"] is True
    assert third["suppressed"] is False


def test_repeated_infrastructure_failure_is_suppressed_then_stops_after_a_change(
    tmp_path: Path,
) -> None:
    runner = _Runner(ExecutedCommand("run tests", 127, "", "tool: not found"))
    adapter = _adapter(tmp_path, runner)

    first = json.loads(adapter.run("tests/example.py"))
    second = json.loads(adapter.run("tests/example.py"))
    (tmp_path / "package-lock.json").write_text("changed", encoding="utf-8")
    third = json.loads(adapter.run("tests/example.py"))
    fourth = json.loads(adapter.run("tests/example.py"))

    assert first["suppressed"] is False
    assert second["suppressed"] is True
    assert third["suppressed"] is False
    assert first["signature"] == third["signature"]
    assert fourth["signature"] == third["signature"]
    assert fourth["stop_loop"] is True
    assert fourth["classification"] == "infrastructure_failure"
    assert len(runner.calls) == 2


def test_targeted_diagnostics_persist_stderr_and_report_under_nested_attempt_id(
    tmp_path: Path,
) -> None:
    """All three truncated streams persist under nested Attempt ids and read back."""
    from coding_agent.runtime.result_capping import build_read_result_slice_tool

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = _Runner(ExecutedCommand("run tests", 1, "x" * 200, "y" * 200))
    runner.report = "<testsuite><testcase classname=\"Example\" name=\"ok\"/>" + "z" * 200 + "</testsuite>"
    context = CommandContext(runner, tmp_path, tmp_path, workspace_root=tmp_path)
    adapter = TargetedTestAdapter(
        _profile(),
        context,
        artifact_store=store,
        inline_limit=80,
        attempt_id="#55/1",
    )

    payload = json.loads(adapter.run("tests/example.py"))

    by_suffix = {item["artifact_id"].split("/")[-1]: item["artifact_id"] for item in payload["artifacts"]}
    assert set(by_suffix) == {"stdout", "stderr", "report"}
    for suffix, artifact_id in by_suffix.items():
        assert artifact_id == f"#55/1/targeted/1/{suffix}"
    assert payload["stdout"]["truncated"] is True
    assert payload["stderr"]["truncated"] is True
    assert store.read(by_suffix["stdout"]) == "x" * 200
    assert store.read(by_suffix["stderr"]) == "y" * 200
    assert "z" * 200 in store.read(by_suffix["report"])
    # The truncated payload stays bounded while the full streams stay
    # readable through the bounded range reader over the same store.
    assert len(json.dumps(payload).encode("utf-8")) <= 4_000
    reader = build_read_result_slice_tool(store)
    assert reader.invoke({"artifact_id": by_suffix["stdout"], "start": 0, "end": 200}) == "x" * 200
    assert reader.invoke({"artifact_id": by_suffix["stderr"], "start": 0, "end": 200}) == "y" * 200


def test_targeted_diagnostics_nested_artifact_ids_cannot_escape_the_store_root(
    tmp_path: Path,
) -> None:
    """Nested Attempt ids persist inside the root; out-of-root ids stay rejected."""
    from coding_agent.runtime.result_capping import ArtifactNotFound

    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = _Runner(ExecutedCommand("run tests", 1, "x" * 200, ""))
    context = CommandContext(runner, tmp_path, tmp_path, workspace_root=tmp_path)
    adapter = TargetedTestAdapter(
        _profile(),
        context,
        artifact_store=store,
        inline_limit=80,
        attempt_id="#55/1",
    )

    payload = json.loads(adapter.run("tests/example.py"))
    artifact_id = payload["artifacts"][0]["artifact_id"]
    assert artifact_id == "#55/1/targeted/1/stdout"
    assert store.read(artifact_id) == "x" * 200
    # The nested artifact lives under the store root, not beside it.
    assert (tmp_path / "artifacts" / "#55" / "1" / "targeted" / "1" / "stdout").is_file()

    with pytest.raises(ArtifactNotFound):
        store.read("../secret.txt")
    with pytest.raises(ArtifactNotFound):
        store.store("../escaped.txt", "content")
    assert not (tmp_path / "escaped.txt").exists()
