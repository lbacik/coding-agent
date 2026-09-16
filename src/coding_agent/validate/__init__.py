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
from coding_agent.validate.diagnostics import (
    DiagnosticArtifact,
    TargetedClassification,
    TargetedDiagnostic,
    TargetedTestAdapter,
    diagnostic_requests_loop_stop,
    redact_diagnostic,
)
from coding_agent.validate.harness import (
    CommandBaseRevisionRunner,
    CommandContext,
    run_test_all,
    run_targeted_test,
    run_validation_contract,
    validate,
)
from coding_agent.validate.junit import JUnitResult, MalformedJUnitReport, parse_junit_xml
from coding_agent.validate.results import Outcome, RawCommandResult, classify
from coding_agent.validate.runner import (
    CommandRunner,
    CredentialStrippedCommandRunner,
    ExecutedCommand,
    SubprocessCommandRunner,
)

__all__ = [
    "BaseRevisionRunner",
    "BaselineFailure",
    "CommandBaseRevisionRunner",
    "CommandContext",
    "CommandEvidence",
    "CommandRunner",
    "CredentialStrippedCommandRunner",
    "DiagnosticArtifact",
    "ExecutedCommand",
    "JUnitResult",
    "MalformedJUnitReport",
    "MissingEvidence",
    "Outcome",
    "RawCommandResult",
    "Regression",
    "SubprocessCommandRunner",
    "TargetedClassification",
    "TargetedDiagnostic",
    "TargetedTestAdapter",
    "Unclaimable",
    "ValidationEvidence",
    "classify",
    "diagnostic_requests_loop_stop",
    "evaluate_validation",
    "parse_junit_xml",
    "redact_diagnostic",
    "run_targeted_test",
    "run_test_all",
    "run_validation_contract",
    "validate",
]
