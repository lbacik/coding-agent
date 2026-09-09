from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from coding_agent.validate.results import Outcome, RawCommandResult, classify


class BaseRevisionRunner(Protocol):
    """Re-runs one named Validation Contract command against the Base
    Revision and returns its result. Checking out that revision is the
    caller's concern (a real worktree, S2c/S4); this harness only decides
    *when* to call it — lazily, and only for a command that failed at the
    Delivery Snapshot (contract §8, L2-7)."""

    def run_at_base(self, command_name: str) -> RawCommandResult: ...


@dataclass(frozen=True)
class CommandEvidence:
    """One command's Validation Evidence at the Delivery Snapshot (contract
    §8): the command, its exit code, how many tests actually executed, and
    the identifier of every individual failure it named."""

    name: str
    command: str
    exit_code: int
    executed: int | None
    """`None` when the command declares no evidence (an unevidenced check,
    contract §7) — there is nothing to count."""
    failure_ids: frozenset[str] | None
    """`None` when the command's evidence can't name individual failures —
    either none was declared, or the declared file could not be read or
    parsed."""


def _command_evidence(result: RawCommandResult) -> CommandEvidence:
    return CommandEvidence(
        name=result.name,
        command=result.command,
        exit_code=result.exit_code,
        executed=result.junit.executed if result.junit is not None else None,
        failure_ids=result.junit.failure_ids if result.evidence_declared and result.junit is not None else None,
    )


@dataclass(frozen=True)
class BaselineFailure:
    """A command's failure set at the Delivery Snapshot is a subset of its
    failure set at the Base Revision (contract §8, ADR 0004): excused, not
    this Attempt's debt."""

    command_name: str
    failure_ids: frozenset[str]


@dataclass(frozen=True)
class Regression:
    """A command names at least one failure at the Delivery Snapshot that
    was not present at the Base Revision. Blocks; the command is not
    excused, even where some of its failures are a Baseline Failure."""

    command_name: str
    new_failure_ids: frozenset[str]
    """Empty when the command has no evidence to name the new failure by —
    still a regression, because the command was clean at the Base Revision
    and is not clean now."""


@dataclass(frozen=True)
class Unclaimable:
    """The command cannot name its individual failures and is red at both
    the Delivery Snapshot and the Base Revision (contract §8, L2-6): no
    Baseline Failure can be established, and none is claimed."""

    command_name: str


@dataclass(frozen=True)
class MissingEvidence:
    """Contract §8, L2-3: a command's evidence names zero executed tests,
    or could not be read at all — a failed validation despite exit code
    zero, and never excused by the baseline."""

    command_name: str


@dataclass(frozen=True)
class ValidationEvidence:
    """The `validate` harness node's output (contract §8): Validation
    Evidence in the Run Ledger. Never the model's own account of a test
    run.

    `commands` is the complete per-command record contract §8 asks for —
    every command, its exit code and executed-test count, and the
    identifiers it named — for the Delivery Snapshot. `baseline_failures`,
    `regressions`, `unclaimable` and `missing_evidence` are this harness's
    judgement about each command that did not simply pass, computed from
    that record and, lazily, the Base Revision.
    """

    commands: tuple[CommandEvidence, ...]
    passed: tuple[str, ...]
    baseline_failures: tuple[BaselineFailure, ...]
    regressions: tuple[Regression, ...]
    unclaimable: tuple[Unclaimable, ...]
    missing_evidence: tuple[MissingEvidence, ...]

    @property
    def clean(self) -> bool:
        return not (self.regressions or self.unclaimable or self.missing_evidence)


_FailureVerdict = BaselineFailure | Regression | Unclaimable


def _evaluate_failure(
    result: RawCommandResult, outcome: Outcome, base_runner: BaseRevisionRunner
) -> _FailureVerdict:
    base_result = base_runner.run_at_base(result.name)
    base_outcome = classify(base_result)

    if outcome == "named-failure":
        if result.junit is None:
            raise AssertionError("unreachable: classify() only returns 'named-failure' with junit evidence present")
        delivery_ids = result.junit.failure_ids

        if base_outcome == "named-failure":
            if base_result.junit is None:
                raise AssertionError(
                    "unreachable: classify() only returns 'named-failure' with junit evidence present"
                )
            new_ids = delivery_ids - base_result.junit.failure_ids
            if new_ids:
                return Regression(result.name, frozenset(new_ids))
            return BaselineFailure(result.name, delivery_ids)
        if base_outcome == "passed":
            # Clean at the Base Revision, failing now: unambiguously new.
            return Regression(result.name, delivery_ids)
        return Unclaimable(result.name)

    # "unnamed-failure": no identifiers on the delivery side at all.
    if base_outcome == "passed":
        return Regression(result.name, frozenset())
    return Unclaimable(result.name)


def evaluate_validation(
    delivery: Sequence[RawCommandResult], base_runner: BaseRevisionRunner
) -> ValidationEvidence:
    """Turn one Delivery Snapshot run into Validation Evidence, re-running
    the Base Revision lazily — only for commands that failed, and only
    those (contract §8, L2-7)."""
    passed: list[str] = []
    baseline_failures: list[BaselineFailure] = []
    regressions: list[Regression] = []
    unclaimable: list[Unclaimable] = []
    missing_evidence: list[MissingEvidence] = []

    for result in delivery:
        outcome = classify(result)

        if outcome == "passed":
            passed.append(result.name)
            continue
        if outcome == "missing-evidence":
            missing_evidence.append(MissingEvidence(result.name))
            continue

        verdict = _evaluate_failure(result, outcome, base_runner)
        if isinstance(verdict, BaselineFailure):
            baseline_failures.append(verdict)
        elif isinstance(verdict, Regression):
            regressions.append(verdict)
        else:
            unclaimable.append(verdict)

    return ValidationEvidence(
        commands=tuple(_command_evidence(result) for result in delivery),
        passed=tuple(passed),
        baseline_failures=tuple(baseline_failures),
        regressions=tuple(regressions),
        unclaimable=tuple(unclaimable),
        missing_evidence=tuple(missing_evidence),
    )
