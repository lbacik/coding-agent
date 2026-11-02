"""The baseline: bootstrap, ``test_all`` and every check at the Base Revision.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 ("After Readiness,
``baseline`` runs ``bootstrap``, then ``test_all`` and every check, on a tree
at the Base Revision, before any model call. A bootstrap failure is an
infrastructure-class failure of class ``setup``. A red baseline ends
``failed`` / ``red-baseline``. Red means a non-zero exit or any reported test
failure. A suite that executes zero tests at the baseline is not red. A check
without an ``evidence`` block is judged by its exit code alone. Baseline
Failure exclusion is deferred, so a red baseline is refused, never worked
around") and §14 (the Terminal Outcome and its reason).

MVP row ids: P-RDY-4, C-RDY-3, C-RDY-4, L2-1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.substitution import render_command
from coding_agent.validate.harness import CommandContext, run_validation_contract
from coding_agent.validate.results import RawCommandResult
from coding_agent.validate.runner import CommandRunner

FAILED = "failed"


@dataclass(frozen=True)
class BaselineReport:
    """What the pinned Validation Contract's commands did at the Base Revision.

    ``bootstrap`` carries no evidence (it is judged by its exit code alone,
    like an unevidenced check). ``results`` holds ``test_all`` then every
    check in profile order. Each entry records its command, exit code,
    executed-test count (``None`` where no evidence was declared or none
    could be read) and individual failure identifiers (``None`` where the
    command names none) — the Validation Evidence L2-1 asks for.
    """

    bootstrap: RawCommandResult
    results: tuple[RawCommandResult, ...]


def run_baseline(
    profile: ProjectProfile,
    runner: CommandRunner,
    working_directory: Path,
    evidence_dir: Path,
) -> BaselineReport:
    """Run the pinned contract's ``bootstrap``, then ``test_all`` and every check.

    The caller holds the tree at the Base Revision (``readiness`` rebuilt it
    and no model has run since); this function only runs commands and reads
    the evidence they declare. ``test_targeted`` is never run here: it is the
    model's own tool during implementation, not part of the baseline.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_command = render_command(profile.bootstrap, evidence_dir=evidence_dir)
    executed = runner.run(bootstrap_command, cwd=working_directory)
    bootstrap = RawCommandResult(
        name="bootstrap",
        command=bootstrap_command,
        exit_code=executed.exit_code,
        junit=None,
        evidence_declared=False,
        stdout=executed.stdout,
        stderr=executed.stderr,
    )
    if bootstrap.exit_code != 0:
        return BaselineReport(bootstrap=bootstrap, results=())
    context = CommandContext(runner, working_directory, evidence_dir)
    return BaselineReport(
        bootstrap=bootstrap, results=run_validation_contract(profile, context)
    )


def baseline_outcome(report: BaselineReport) -> tuple[str, str] | None:
    """Decide the Attempt's fate from a baseline run.

    Returns ``("failed", reason)`` for a bootstrap failure (C-RDY-3:
    ``failed`` / ``infrastructure-failure``, class ``setup``) or a red
    baseline (P-RDY-4: ``failed`` / ``red-baseline``, naming the failing
    commands and, where evidence provides them, the individual failure
    identifiers), and ``None`` when the Attempt proceeds to its first step —
    including a ``test_all`` that executes zero tests and exits zero
    (C-RDY-4), and treating a check without an ``evidence`` block by its exit
    code alone.
    """
    if report.bootstrap.exit_code != 0:
        return (
            FAILED,
            "infrastructure-failure: setup "
            f"bootstrap {report.bootstrap.command!r} "
            f"exited {report.bootstrap.exit_code}",
        )
    failing: list[str] = []
    failure_ids: set[str] = set()
    for result in report.results:
        red = result.exit_code != 0 or (
            result.evidence_declared
            and result.junit is not None
            and bool(result.junit.failure_ids)
        )
        if not red:
            continue
        failing.append(result.name)
        if result.evidence_declared and result.junit is not None:
            failure_ids.update(result.junit.failure_ids)
    if not failing:
        return None
    reason = f"red-baseline: {', '.join(failing)}"
    if failure_ids:
        reason += f"; failures: {', '.join(sorted(failure_ids))}"
    return (FAILED, reason)


def baseline_evidence_payload(report: BaselineReport) -> dict[str, object]:
    """Validation Evidence for the Run Ledger (L2-1): every baseline command,
    its exit code, how many tests actually executed, and the identifier of
    every individual failure it named."""

    def entry(result: RawCommandResult) -> dict[str, object]:
        return {
            "name": result.name,
            "command": result.command,
            "exit_code": result.exit_code,
            "executed": result.junit.executed if result.junit is not None else None,
            "failure_ids": (
                sorted(result.junit.failure_ids)
                if result.evidence_declared and result.junit is not None
                else None
            ),
        }

    return {
        "bootstrap": entry(report.bootstrap),
        "commands": [entry(result) for result in report.results],
    }
