from __future__ import annotations

from coding_agent.validate.baseline import (
    BaselineFailure,
    BaseRevisionRunner,
    CommandEvidence,
    MissingEvidence,
    Regression,
    Unclaimable,
    ValidationEvidence,
    evaluate_validation,
)
from coding_agent.validate.harness import (
    CommandBaseRevisionRunner,
    CommandContext,
    run_validation_contract,
    validate,
)
from coding_agent.validate.junit import JUnitResult, MalformedJUnitReport, parse_junit_xml
from coding_agent.validate.results import Outcome, RawCommandResult, classify
from coding_agent.validate.runner import CommandRunner, ExecutedCommand, SubprocessCommandRunner

__all__ = [
    "BaseRevisionRunner",
    "BaselineFailure",
    "CommandBaseRevisionRunner",
    "CommandContext",
    "CommandEvidence",
    "CommandRunner",
    "ExecutedCommand",
    "JUnitResult",
    "MalformedJUnitReport",
    "MissingEvidence",
    "Outcome",
    "RawCommandResult",
    "Regression",
    "SubprocessCommandRunner",
    "Unclaimable",
    "ValidationEvidence",
    "classify",
    "evaluate_validation",
    "parse_junit_xml",
    "run_validation_contract",
    "validate",
]
