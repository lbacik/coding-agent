from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from coding_agent.profile.schema import Check, Evidence, ProjectProfile, Toolchain
from coding_agent.profile.substitution import render_command
from coding_agent.validate import (
    CommandBaseRevisionRunner,
    CommandContext,
    ExecutedCommand,
    MalformedJUnitReport,
    RawCommandResult,
    classify,
    evaluate_validation,
    parse_junit_xml,
    run_targeted_test,
    run_validation_contract,
    validate,
)
from coding_agent.validate.runner import SubprocessCommandRunner


def _junit_report(cases: dict[str, bool]) -> str:
    """cases: test name -> failed? All under one classname, `pkg`."""
    parts = [f'<testsuite name="suite" tests="{len(cases)}">']
    for name, failed in cases.items():
        if failed:
            parts.append(f'<testcase classname="pkg" name="{name}"><failure message="boom"/></testcase>')
        else:
            parts.append(f'<testcase classname="pkg" name="{name}"/>')
    parts.append("</testsuite>")
    return "".join(parts)


def _profile(**overrides: object) -> ProjectProfile:
    base: dict[str, object] = dict(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="uv sync --frozen",
        test_all="uv run pytest -q --junit-xml={evidence_dir}/test_all.xml",
        test_targeted="uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}",
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/test_all.xml",
            test_targeted="{evidence_dir}/test_targeted.xml",
        ),
        checks=(),
        services=(),
    )
    base.update(overrides)
    return ProjectProfile(**base)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ScriptedRun:
    exit_code: int
    evidence_path: Path | None = None
    evidence_text: str | None = None


class FakeCommandRunner:
    """A `CommandRunner` fake keyed by `(command, cwd)`: a real command is a
    shell process in a directory, and the Base Revision re-run of contract
    §8's lazy comparison happens in a *different* directory (a different
    checkout) — so the same rendered command must be scriptable
    independently for the Delivery Snapshot and the Base Revision."""

    def __init__(self) -> None:
        self._scripts: dict[tuple[str, str], ScriptedRun] = {}
        self.calls: list[tuple[str, str]] = []

    def script(self, command: str, cwd: Path, run: ScriptedRun) -> None:
        self._scripts[(command, str(cwd))] = run

    def run(self, command: str, *, cwd: Path) -> ExecutedCommand:
        self.calls.append((command, str(cwd)))
        run = self._scripts[(command, str(cwd))]
        if run.evidence_path is not None and run.evidence_text is not None:
            run.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            run.evidence_path.write_text(run.evidence_text, encoding="utf-8")
        return ExecutedCommand(command=command, exit_code=run.exit_code, stdout="", stderr="")


# --- parse_junit_xml ---------------------------------------------------------


def test_parses_executed_count_and_failure_identifiers() -> None:
    result = parse_junit_xml(_junit_report({"A": False, "B": True, "C": False}))
    assert result.executed == 3
    assert result.failure_ids == frozenset({"pkg::B"})


def test_a_suite_collecting_zero_tests_has_zero_executed() -> None:
    result = parse_junit_xml('<testsuite name="suite" tests="0"></testsuite>')
    assert result.executed == 0
    assert result.failure_ids == frozenset()


def test_a_skipped_testcase_counts_toward_neither_executed_nor_failed() -> None:
    xml = (
        '<testsuite name="suite" tests="2">'
        '<testcase classname="pkg" name="A"><skipped/></testcase>'
        '<testcase classname="pkg" name="B"/>'
        "</testsuite>"
    )
    result = parse_junit_xml(xml)
    assert result.executed == 1
    assert result.failure_ids == frozenset()


def test_a_testsuites_wrapper_is_accepted_like_a_bare_testsuite() -> None:
    xml = (
        '<testsuites>'
        '<testsuite name="a" tests="1">'
        '<testcase classname="pkg" name="A"><error message="boom"/></testcase>'
        "</testsuite>"
        "</testsuites>"
    )
    result = parse_junit_xml(xml)
    assert result.executed == 1
    assert result.failure_ids == frozenset({"pkg::A"})


def test_malformed_xml_raises_a_named_exception() -> None:
    with pytest.raises(MalformedJUnitReport):
        parse_junit_xml("not xml at all <<<")


def test_an_unexpected_root_element_raises_a_named_exception() -> None:
    with pytest.raises(MalformedJUnitReport):
        parse_junit_xml('<report></report>')


# --- classify -----------------------------------------------------------------


def test_classify_passed_when_evidence_declared_and_clean() -> None:
    result = RawCommandResult(
        name="test_all", command="x", exit_code=0,
        junit=parse_junit_xml(_junit_report({"A": False})), evidence_declared=True,
    )
    assert classify(result) == "passed"


def test_classify_missing_evidence_when_zero_executed_despite_exit_zero() -> None:
    result = RawCommandResult(
        name="test_all", command="x", exit_code=0,
        junit=parse_junit_xml('<testsuite tests="0"></testsuite>'), evidence_declared=True,
    )
    assert classify(result) == "missing-evidence"


def test_classify_missing_evidence_when_declared_evidence_could_not_be_read() -> None:
    result = RawCommandResult(name="test_all", command="x", exit_code=1, junit=None, evidence_declared=True)
    assert classify(result) == "missing-evidence"


def test_classify_named_failure_when_evidence_names_a_failing_test() -> None:
    result = RawCommandResult(
        name="test_all", command="x", exit_code=1,
        junit=parse_junit_xml(_junit_report({"A": True})), evidence_declared=True,
    )
    assert classify(result) == "named-failure"


def test_classify_unnamed_failure_when_no_evidence_is_declared_and_exit_is_nonzero() -> None:
    result = RawCommandResult(name="types", command="x", exit_code=1, junit=None, evidence_declared=False)
    assert classify(result) == "unnamed-failure"


def test_classify_passed_when_no_evidence_is_declared_and_exit_is_zero() -> None:
    result = RawCommandResult(name="types", command="x", exit_code=0, junit=None, evidence_declared=False)
    assert classify(result) == "passed"


# --- L2-3: a suite collecting zero tests --------------------------------------


def test_l2_3_a_suite_collecting_zero_tests_fails_validation_despite_exit_code_zero(tmp_path: Path) -> None:
    profile = _profile()
    working_directory = tmp_path / "delivery"
    working_directory.mkdir()
    evidence_dir = tmp_path / "evidence"

    runner = FakeCommandRunner()
    command = render_command(profile.test_all, evidence_dir=evidence_dir)
    runner.script(
        command,
        working_directory,
        ScriptedRun(
            exit_code=0,
            evidence_path=evidence_dir / "test_all.xml",
            evidence_text='<testsuite name="suite" tests="0"></testsuite>',
        ),
    )

    class _UnreachableBaseRunner:
        def run_at_base(self, command_name: str) -> RawCommandResult:
            raise AssertionError("missing evidence must not trigger a Base Revision re-run")

    context = CommandContext(runner, working_directory, evidence_dir)
    evidence = validate(profile, context, _UnreachableBaseRunner())
    assert evidence.clean is False
    assert [m.command_name for m in evidence.missing_evidence] == ["test_all"]
    assert evidence.regressions == ()
    assert evidence.baseline_failures == ()

    # Contract §8: exit code and executed count are retained even though
    # the command excuses nothing.
    [command_evidence] = evidence.commands
    assert command_evidence.exit_code == 0
    assert command_evidence.executed == 0


# --- L2-4, L2-5, L2-7: baseline comparison end to end via the real harness --


def _wire_test_all(
    profile: ProjectProfile,
    runner: FakeCommandRunner,
    *,
    working_directory: Path,
    evidence_dir: Path,
    exit_code: int,
    cases: dict[str, bool],
) -> None:
    command = render_command(profile.test_all, evidence_dir=evidence_dir)
    runner.script(
        command,
        working_directory,
        ScriptedRun(exit_code=exit_code, evidence_path=evidence_dir / "test_all.xml", evidence_text=_junit_report(cases)),
    )


def test_l2_4_a_new_failure_at_the_candidate_is_a_regression_and_the_command_is_not_excused(
    tmp_path: Path,
) -> None:
    profile = _profile()
    delivery_dir, base_dir = tmp_path / "delivery", tmp_path / "base"
    delivery_evidence, base_evidence = tmp_path / "delivery-evidence", tmp_path / "base-evidence"
    delivery_dir.mkdir()
    base_dir.mkdir()

    runner = FakeCommandRunner()
    # Base fails test A; candidate fails A and B (ADR 0004's load-bearing case).
    _wire_test_all(profile, runner, working_directory=base_dir, evidence_dir=base_evidence, exit_code=1, cases={"A": True, "B": False})
    _wire_test_all(profile, runner, working_directory=delivery_dir, evidence_dir=delivery_evidence, exit_code=1, cases={"A": True, "B": True})

    base_runner = CommandBaseRevisionRunner(profile, CommandContext(runner, base_dir, base_evidence))
    evidence = validate(profile, CommandContext(runner, delivery_dir, delivery_evidence), base_runner)

    assert evidence.clean is False
    assert evidence.baseline_failures == ()
    assert len(evidence.regressions) == 1
    regression = evidence.regressions[0]
    assert regression.command_name == "test_all"
    assert regression.new_failure_ids == frozenset({"pkg::B"})

    # The Delivery Snapshot's exit code, executed count and full failure
    # set are still on record (contract §8), even though the command is
    # not excused.
    [command_evidence] = evidence.commands
    assert command_evidence.exit_code == 1
    assert command_evidence.executed == 2
    assert command_evidence.failure_ids == frozenset({"pkg::A", "pkg::B"})


def test_l2_5_a_failure_present_at_the_base_is_excused_as_a_baseline_failure(tmp_path: Path) -> None:
    profile = _profile()
    delivery_dir, base_dir = tmp_path / "delivery", tmp_path / "base"
    delivery_evidence, base_evidence = tmp_path / "delivery-evidence", tmp_path / "base-evidence"
    delivery_dir.mkdir()
    base_dir.mkdir()

    runner = FakeCommandRunner()
    _wire_test_all(profile, runner, working_directory=base_dir, evidence_dir=base_evidence, exit_code=1, cases={"A": True, "B": False})
    _wire_test_all(profile, runner, working_directory=delivery_dir, evidence_dir=delivery_evidence, exit_code=1, cases={"A": True, "B": False})

    base_runner = CommandBaseRevisionRunner(profile, CommandContext(runner, base_dir, base_evidence))
    evidence = validate(profile, CommandContext(runner, delivery_dir, delivery_evidence), base_runner)

    assert evidence.clean is True
    assert evidence.regressions == ()
    assert len(evidence.baseline_failures) == 1
    baseline = evidence.baseline_failures[0]
    assert baseline.command_name == "test_all"
    assert baseline.failure_ids == frozenset({"pkg::A"})


def test_l2_7_the_base_is_re_run_only_for_commands_that_failed_at_the_delivery_snapshot(tmp_path: Path) -> None:
    profile = _profile(checks=(Check(name="types", command="mypy", evidence=None),))

    delivery_dir, base_dir = tmp_path / "delivery", tmp_path / "base"
    delivery_evidence, base_evidence = tmp_path / "delivery-evidence", tmp_path / "base-evidence"
    delivery_dir.mkdir()
    base_dir.mkdir()

    runner = FakeCommandRunner()
    # test_all passes at delivery: must never be re-run at the base.
    _wire_test_all(profile, runner, working_directory=delivery_dir, evidence_dir=delivery_evidence, exit_code=0, cases={"A": False})
    # the "types" check fails at delivery: must be re-run at the base.
    runner.script("mypy", delivery_dir, ScriptedRun(exit_code=1))
    runner.script("mypy", base_dir, ScriptedRun(exit_code=0))

    base_runner = CommandBaseRevisionRunner(profile, CommandContext(runner, base_dir, base_evidence))
    evidence = validate(profile, CommandContext(runner, delivery_dir, delivery_evidence), base_runner)

    assert evidence.clean is False
    assert [r.command_name for r in evidence.regressions] == ["types"]
    assert ("mypy", str(base_dir)) in runner.calls
    test_all_base_command = render_command(profile.test_all, evidence_dir=base_evidence)
    assert (test_all_base_command, str(base_dir)) not in runner.calls


# --- L2-6: a command that cannot name its individual failures ---------------


def test_l2_6_a_command_with_no_identifiers_red_at_both_ends_establishes_no_baseline_failure(
    tmp_path: Path,
) -> None:
    profile = _profile(checks=(Check(name="types", command="mypy", evidence=None),))
    delivery_dir, base_dir = tmp_path / "delivery", tmp_path / "base"
    delivery_evidence, base_evidence = tmp_path / "delivery-evidence", tmp_path / "base-evidence"
    delivery_dir.mkdir()
    base_dir.mkdir()

    runner = FakeCommandRunner()
    _wire_test_all(profile, runner, working_directory=delivery_dir, evidence_dir=delivery_evidence, exit_code=0, cases={"A": False})
    runner.script("mypy", delivery_dir, ScriptedRun(exit_code=1))
    runner.script("mypy", base_dir, ScriptedRun(exit_code=1))

    base_runner = CommandBaseRevisionRunner(profile, CommandContext(runner, base_dir, base_evidence))
    evidence = validate(profile, CommandContext(runner, delivery_dir, delivery_evidence), base_runner)

    assert evidence.clean is False
    assert evidence.regressions == ()
    assert evidence.baseline_failures == ()
    assert [u.command_name for u in evidence.unclaimable] == ["types"]


def test_l2_8_checks_none_leaves_nothing_to_run_or_block_on(tmp_path: Path) -> None:
    profile = _profile(checks=())
    working_directory = tmp_path / "delivery"
    working_directory.mkdir()
    evidence_dir = tmp_path / "evidence"

    runner = FakeCommandRunner()
    _wire_test_all(profile, runner, working_directory=working_directory, evidence_dir=evidence_dir, exit_code=0, cases={"A": False})

    class _UnreachableBaseRunner:
        def run_at_base(self, command_name: str) -> RawCommandResult:
            raise AssertionError("a passing test_all with no checks must never need a Base Revision run")

    evidence = validate(profile, CommandContext(runner, working_directory, evidence_dir), _UnreachableBaseRunner())
    assert evidence.clean is True
    assert evidence.passed == ("test_all",)


# --- run_validation_contract: runs test_all and every named check -----------


def test_run_validation_contract_runs_test_all_and_every_named_check(tmp_path: Path) -> None:
    profile = _profile(checks=(Check(name="types", command="mypy src", evidence=None),))
    working_directory = tmp_path / "delivery"
    working_directory.mkdir()
    evidence_dir = tmp_path / "evidence"

    runner = FakeCommandRunner()
    _wire_test_all(profile, runner, working_directory=working_directory, evidence_dir=evidence_dir, exit_code=0, cases={"A": False})
    runner.script("mypy src", working_directory, ScriptedRun(exit_code=0))

    results = run_validation_contract(profile, CommandContext(runner, working_directory, evidence_dir))
    assert {r.name for r in results} == {"test_all", "types"}


# --- L2-2: run_targeted_test runs only the named test file ------------------


def test_run_targeted_test_renders_the_path_into_the_command_and_evidence(tmp_path: Path) -> None:
    profile = _profile()
    working_directory = tmp_path / "delivery"
    working_directory.mkdir()
    evidence_dir = tmp_path / "evidence"

    runner = FakeCommandRunner()
    command = render_command(profile.test_targeted, evidence_dir=evidence_dir, path="tests/test_b.py")
    runner.script(
        command,
        working_directory,
        ScriptedRun(
            exit_code=0,
            evidence_path=evidence_dir / "test_targeted.xml",
            evidence_text=_junit_report({"B": False}),
        ),
    )

    context = CommandContext(runner, working_directory, evidence_dir)
    result = run_targeted_test(profile, context, "tests/test_b.py")

    assert result.name == "test_targeted"
    assert result.command == command
    assert "tests/test_b.py" in result.command
    assert result.junit is not None
    assert result.junit.executed == 1
    assert result.junit.failure_ids == frozenset()
    assert runner.calls == [(command, str(working_directory))]


# --- SubprocessCommandRunner: a light real-process smoke test ---------------


def test_subprocess_command_runner_actually_shells_out(tmp_path: Path) -> None:
    runner = SubprocessCommandRunner()
    result = runner.run("python3 -c \"import sys; sys.exit(3)\"", cwd=tmp_path)
    assert result.exit_code == 3


# --- evaluate_validation: direct, mixed-outcome sanity check ----------------


def test_evaluate_validation_collects_every_outcome_kind_independently() -> None:
    """Lighter than the end-to-end tests above: exercises the aggregation
    logic directly over hand-built RawCommandResults so the mix of outcome
    kinds in one run is covered without wiring a runner for each."""

    class _StubBaseRunner:
        def __init__(self, by_name: dict[str, RawCommandResult]) -> None:
            self._by_name = by_name

        def run_at_base(self, command_name: str) -> RawCommandResult:
            return self._by_name[command_name]

    passing = RawCommandResult(
        name="passing", command="x", exit_code=0, junit=parse_junit_xml(_junit_report({"A": False})), evidence_declared=True
    )
    missing = RawCommandResult(name="missing", command="x", exit_code=0, junit=parse_junit_xml('<testsuite tests="0"></testsuite>'), evidence_declared=True)
    regressing = RawCommandResult(
        name="regressing", command="x", exit_code=1, junit=parse_junit_xml(_junit_report({"A": True})), evidence_declared=True
    )

    base_runner = _StubBaseRunner(
        {
            "regressing": RawCommandResult(
                name="regressing", command="x", exit_code=0, junit=parse_junit_xml(_junit_report({"A": False})), evidence_declared=True
            )
        }
    )

    evidence = evaluate_validation([passing, missing, regressing], base_runner)
    assert evidence.passed == ("passing",)
    assert [m.command_name for m in evidence.missing_evidence] == ["missing"]
    assert [r.command_name for r in evidence.regressions] == ["regressing"]
    assert evidence.clean is False

    # Contract §8: the complete per-command record survives classification,
    # not just the names sorted into each judgement bucket.
    assert {c.name for c in evidence.commands} == {"passing", "missing", "regressing"}
    by_name = {c.name: c for c in evidence.commands}
    assert by_name["passing"].exit_code == 0
    assert by_name["missing"].executed == 0
    assert by_name["regressing"].failure_ids == frozenset({"pkg::A"})
