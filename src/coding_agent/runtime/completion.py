"""Declaring completion: Criterion Evidence and the per-Attempt claim.

Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (completion is
declared and substantiated, never inferred; the runtime owns the evidence;
``report_completion`` is the only non-exceptional exit; its schema is
generated per Attempt from that Attempt's Acceptance Criteria; the
implementation stage ends in exactly one Implementation Outcome decided
from the claim and Criterion Evidence alone; only ``verified completion``
may use PASS wording) and §1 (the model's toolset contains no GitHub
mutation).

MVP row ids: L2-2, L3-IMP-10, L3-IMP-20.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.tools import BaseTool

from coding_agent.validate.junit import JUnitResult

UNCOVERED = "uncovered"
"""The explicit marker a claim uses for a criterion with no covering test."""

ImplementationOutcome = Literal[
    "verified completion",
    "already satisfied",
    "completion not established",
]
"""The M1 Implementation Outcomes (contract §9; M2 adds the rest)."""


@dataclass(frozen=True)
class CriterionRecord:
    """One recorded test execution: its identifier and whether it passed."""

    test_id: str
    passed: bool


@dataclass
class CriterionEvidence:
    """Runtime-owned history of every test the targeted-test tool executed.

    Only the runtime-owned targeted-test tool writes here: it calls
    :meth:`record_junit` with the parsed JUnit result of each invocation.
    Tests the model runs through its shell count for nothing because no
    shell tool writes here and file tools cannot reach the per-Attempt
    ``{evidence_dir}`` the targeted-test tool alone owns. The history is
    kept in order; :meth:`latest_result` reports the latest recorded result
    for one test id, which is what the Implementation Outcome decides on.
    """

    _history: list[CriterionRecord] = field(default_factory=list)
    _latest: dict[str, bool] = field(default_factory=dict)

    def record_junit(self, result: JUnitResult) -> None:
        """Record the ids and results of every test in one JUnit report."""
        for test_id in sorted(result.passed_ids):
            self._history.append(CriterionRecord(test_id=test_id, passed=True))
            self._latest[test_id] = True
        for test_id in sorted(result.failure_ids):
            self._history.append(CriterionRecord(test_id=test_id, passed=False))
            self._latest[test_id] = False

    def latest_result(self, test_id: str) -> bool | None:
        """The latest recorded result for ``test_id``, or ``None``."""
        return self._latest.get(test_id)

    def history(self) -> tuple[CriterionRecord, ...]:
        """Every recorded execution, in the order it ran."""
        return tuple(self._history)

    def executed_ids(self) -> frozenset[str]:
        """Every test id that executed at least once."""
        return frozenset(self._latest)


def normalize_claim(
    raw: Mapping[str, object] | None,
    expected_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...] | None]:
    """Normalize a ``report_completion`` claim to per-criterion coverage.

    Returns one entry per expected ``AC-n`` id: a tuple of covering test
    identifiers, or ``None`` for an explicit ``uncovered`` marker, a missing
    entry, or anything that is not a non-empty list of non-empty strings.
    An explicitly uncovered criterion is accepted, not refused (contract
    §9): it yields ``completion not established`` / ``criteria-uncovered``.
    """
    claim: dict[str, tuple[str, ...] | None] = {}
    source: Mapping[str, object] = raw or {}
    for criterion_id in expected_ids:
        value = source.get(criterion_id, UNCOVERED)
        if isinstance(value, str) and value == UNCOVERED:
            claim[criterion_id] = None
            continue
        if isinstance(value, (list, tuple)) and value:
            names: list[str] = []
            valid = True
            for item in value:
                if isinstance(item, str) and item:
                    names.append(item)
                else:
                    valid = False
                    break
            if valid and names:
                claim[criterion_id] = tuple(names)
                continue
        claim[criterion_id] = None
    return claim


def decide_implementation_outcome(
    claim: Mapping[str, tuple[str, ...] | None] | None,
    evidence: CriterionEvidence,
    *,
    has_diff: bool,
) -> tuple[ImplementationOutcome, str]:
    """Decide the one Implementation Outcome from claim and evidence alone.

    - ``verified completion``: a candidate diff is present and every
      criterion names at least one test whose latest Criterion Evidence is
      a pass;
    - ``already satisfied``: no candidate diff and every criterion is
      covered by passing Criterion Evidence at the Base Revision;
    - ``completion not established``: with reason ``criteria-uncovered``
      for an explicitly uncovered or otherwise uncovered criterion, or
      ``no-completion-evidence`` where no claim was made.

    An empty diff without a complete per-criterion justification is
    ``completion not established``, never ``already satisfied`` (L3-IMP-10,
    L3-IMP-20).
    """
    if claim is None:
        return ("completion not established", "no-completion-evidence")
    uncovered = False
    for _criterion_id, covering in claim.items():
        if covering is None:
            uncovered = True
            break
        if not any(evidence.latest_result(test_id) is True for test_id in covering):
            uncovered = True
            break
    if not claim:
        uncovered = True
    if uncovered:
        return ("completion not established", "criteria-uncovered")
    if has_diff:
        return ("verified completion", "verified-completion")
    return ("already satisfied", "already-satisfied")


def report_completion_schema(
    criteria_ids: tuple[str, ...],
) -> dict[str, Any]:
    """The JSON schema for the per-Attempt ``report_completion`` tool.

    It lists exactly the Attempt's ``AC-n`` ids: one required property per
    criterion, each either a list of covering test identifiers or the
    explicit ``uncovered`` string.
    """
    properties: dict[str, Any] = {}
    for criterion_id in criteria_ids:
        properties[criterion_id] = {
            "description": (
                f"Test identifiers covering {criterion_id}, "
                f'or the string "{UNCOVERED}" where it has none.'
            ),
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string", "enum": [UNCOVERED]},
            ],
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(criteria_ids),
        "additionalProperties": False,
    }


class ReportCompletionTool(BaseTool):
    """The per-Attempt ``report_completion`` exit tool (contract §9).

    Its schema is generated per Attempt from that Attempt's Acceptance
    Criteria: for each ``AC-n`` id the covering test identifiers or an
    explicit ``uncovered``. The M1 step exits through it; the outcome is
    decided after the step from the recorded claim and Criterion Evidence
    alone, so calling it records the claim and returns an acknowledgement.
    """

    name: str = "report_completion"
    description: str = (
        "Declare the implementation stage finished with per-criterion "
        "coverage. For each AC-n id give the covering test identifiers or "
        'the string "uncovered".'
    )
    args_schema: Any = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    _claim_sink: dict[str, object] = {}

    def __init__(self, criteria_ids: tuple[str, ...], claim_sink: dict[str, object]) -> None:
        super().__init__(
            args_schema=report_completion_schema(criteria_ids),
            description=(
                "Declare the implementation stage finished. "
                f"Criteria: {', '.join(criteria_ids) if criteria_ids else '(none)'}. "
                "For each AC-n id give the covering test identifiers or "
                f'the string "{UNCOVERED}".'
            ),
        )
        object.__setattr__(self, "_claim_sink", claim_sink)
        object.__setattr__(self, "_criteria_ids", tuple(criteria_ids))

    def _run(self, **kwargs: object) -> str:
        sink = object.__getattribute__(self, "_claim_sink")
        assert isinstance(sink, dict)
        sink.clear()
        sink.update(kwargs)
        return "completion recorded"

    async def _arun(self, **kwargs: object) -> str:
        return self._run(**kwargs)


def build_report_completion_tool(
    criteria_ids: tuple[str, ...],
    claim_sink: dict[str, object] | None = None,
) -> ReportCompletionTool:
    """Build the per-Attempt ``report_completion`` tool for ``criteria_ids``."""
    return ReportCompletionTool(criteria_ids, claim_sink if claim_sink is not None else {})
