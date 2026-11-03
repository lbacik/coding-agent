from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from coding_agent.validate.junit import JUnitResult


@dataclass(frozen=True)
class RawCommandResult:
    """What actually happened when one Validation Contract command ran,
    before the harness decides what it means (contract §7, §11).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (a command
    timeout kills the whole process group and is classified the way the node
    already classifies a failure).

    MVP row ids: P-CMD-1.
    """

    name: str
    command: str
    exit_code: int
    junit: JUnitResult | None
    """`None` when the command declares no evidence path (an unevidenced
    check, contract §7) or the declared evidence file could not be read
    or parsed."""
    evidence_declared: bool
    """Whether the Validation Contract declared an evidence path for this
    command at all — distinct from `junit` being `None`, which can also
    mean a declared file that failed to produce evidence."""
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    """Whether the per-command timeout killed the command's group (§11)."""


Outcome = Literal["passed", "missing-evidence", "named-failure", "unnamed-failure"]
"""A command's Delivery Snapshot outcome (contract §8):

- `"passed"`: clean, nothing to excuse.
- `"missing-evidence"`: L2-3 — evidence was declared but names zero
  executed tests, or could not be read at all. A failed validation
  regardless of exit code, and never excused by the baseline.
- `"named-failure"`: failed, and the identifier of every individual
  failure is known.
- `"unnamed-failure"`: failed, but no evidence names which individual
  test or check failed — either none was declared for this command (an
  unevidenced check), or evidence was declared and read but named no
  failure despite a non-zero exit code.
"""


def classify(result: RawCommandResult) -> Outcome:
    if not result.evidence_declared:
        return "passed" if result.exit_code == 0 else "unnamed-failure"

    if result.junit is None or result.junit.executed == 0:
        return "missing-evidence"
    if result.exit_code == 0 and not result.junit.failure_ids:
        return "passed"
    if result.junit.failure_ids:
        return "named-failure"
    return "unnamed-failure"
