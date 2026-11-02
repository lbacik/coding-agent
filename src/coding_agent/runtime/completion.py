"""Declaring completion: Criterion Evidence and the per-Attempt claim.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (a non-final step
ends with ``report_step``: ``done`` or ``cannot-continue``, plus a
justification bounded in size; the final step ends with
``report_completion``; the step boundary composes a Continuation Snapshot
carrying the justification as a bounded field) and §9 (completion is
declared and substantiated, never inferred; the runtime owns the evidence;
``report_completion`` is the only non-exceptional exit of the final step;
its schema is generated per Attempt from that Attempt's Acceptance Criteria;
the implementation stage ends in exactly one Implementation Outcome decided
from the claim and Criterion Evidence alone; only ``verified completion``
may use PASS wording; a claim naming an unbacked test is returned with a
tool result naming every failing entry, and a return is never a progress
event; after two returns the third call is force-accepted; an explicitly
``uncovered`` criterion is accepted, never returned) and §1 (the model's
toolset contains no GitHub mutation; there is no shell tool and file tools
cannot reach the runtime-owned ``{evidence_dir}``).

MVP row ids: L2-2, L3-IMP-10, L3-IMP-20, C-IMP-1, L3-IMP-28, L3-IMP-29,
W-STP-1, W-STP-2, W-STP-5, C-STP-1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.tools import BaseTool

from coding_agent.validate.junit import JUnitResult

UNCOVERED = "uncovered"
"""The explicit marker a claim uses for a criterion with no covering test."""

MAX_CLAIM_RETURNS = 2
"""How many evidence returns precede a force-accept (contract §9).

A failing claim is returned twice; the third ``report_completion`` call is
force-accepted. The budget belongs to the work node: a fresh step subgraph
opens with it restored (M2 handoff ticket owns the restore).
"""

ImplementationOutcome = Literal[
    "verified completion",
    "implemented but unverified",
    "already satisfied",
    "completion not established",
]
"""The M2 Implementation Outcomes (contract §9)."""


@dataclass(frozen=True)
class CriterionRecord:
    """One recorded test execution: its identifier and whether it passed."""

    test_id: str
    passed: bool


@dataclass(frozen=True)
class ClaimFailure:
    """One covering test the Criterion Evidence does not back (contract §9).

    ``reason`` is either ``absent from Criterion Evidence`` (the identifier
    was never recorded, so it never executed through the runtime-owned
    targeted-test tool) or ``latest recorded run did not pass``.
    """

    criterion_id: str
    test_id: str
    reason: str


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


@dataclass
class ClaimRecord:
    """The work node's claim-rejection budget and accepted claim (contract §9).

    ``returns`` counts evidence returns so far; once it reaches
    ``MAX_CLAIM_RETURNS`` the next call is force-accepted instead of
    returned (so the forcing call itself increments it past the budget).
    ``accepted_raw`` holds the raw
    arguments of the accepted (or force-accepted) ``report_completion`` call,
    or ``None`` where no call has been accepted yet. ``force_accepted`` is
    true only where the accepted call was forced after the budget ran out.
    The record belongs to the work node: a fresh step subgraph opens with a
    fresh record.
    """

    returns: int = 0
    accepted_raw: dict[str, object] | None = None
    force_accepted: bool = False


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


def find_evidence_failures(
    claim: Mapping[str, tuple[str, ...] | None],
    evidence: CriterionEvidence,
) -> list[ClaimFailure]:
    """List every covering test the Criterion Evidence does not back.

    An explicitly ``uncovered`` criterion (``None``) is accepted, never
    returned (contract §9, L3-IMP-29): it contributes no failure here and
    instead yields ``criteria-uncovered`` at decision time. Any other test
    identifier whose latest recorded result is not a pass fails: ``None``
    (absent from Criterion Evidence, so never executed through the
    runtime-owned tool) or ``False`` (its latest recorded run did not pass).
    """
    failures: list[ClaimFailure] = []
    for criterion_id in sorted(claim):
        covering = claim[criterion_id]
        if covering is None:
            continue
        for test_id in covering:
            latest = evidence.latest_result(test_id)
            if latest is True:
                continue
            if latest is None:
                reason = "absent from Criterion Evidence"
            else:
                reason = "latest recorded run did not pass"
            failures.append(
                ClaimFailure(
                    criterion_id=criterion_id, test_id=test_id, reason=reason
                )
            )
    return failures


def format_claim_return(failures: list[ClaimFailure], *, returns_so_far: int) -> str:
    """Render the tool result returning a claim (contract §9, C-IMP-1).

    The result names every failing entry so the model can react: one line
    per ``(criterion, test)`` pair with its reason. The return count is
    included so the model sees the budget (two returns, then force-accept).
    """
    lines = [
        f"Completion claim returned ({returns_so_far + 1}/{MAX_CLAIM_RETURNS + 1} "
        "before force-accept): the following covering tests are not backed "
        "by Criterion Evidence:"
    ]
    for failure in failures:
        lines.append(
            f"- {failure.criterion_id}: {failure.test_id} "
            f"({failure.reason})"
        )
    lines.append(
        "Run the covering tests through the test_targeted tool, or mark the "
        f'criterion "{UNCOVERED}" where it has no covering test.'
    )
    return "\n".join(lines)


def decide_implementation_outcome(
    claim: Mapping[str, tuple[str, ...] | None] | None,
    evidence: CriterionEvidence,
    *,
    has_diff: bool,
    force_accepted: bool = False,
) -> tuple[ImplementationOutcome, str]:
    """Decide the one Implementation Outcome from claim and evidence alone.

    - ``verified completion``: claim accepted without force; a candidate
      diff is present and every criterion names at least one test whose
      latest Criterion Evidence result is a pass;
    - ``implemented but unverified``: claim force-accepted after two
      returns; a candidate diff is present;
    - ``already satisfied``: no candidate diff and every criterion is
      covered by passing Criterion Evidence at the Base Revision;
    - ``completion not established``: with reason ``criteria-uncovered``
      for an explicitly uncovered or otherwise uncovered criterion,
      ``claims-unverified`` for a force-accepted claim with no candidate
      diff, or ``no-completion-evidence`` where no claim was made.

    An empty diff without a complete per-criterion justification is
    ``completion not established``, never ``already satisfied`` (L3-IMP-10,
    L3-IMP-20).
    """
    if force_accepted:
        if has_diff:
            return ("implemented but unverified", "unverified")
        return ("completion not established", "claims-unverified")
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
    explicit ``uncovered``. The M2 step exits through it; the outcome is
    decided after the step from the recorded claim and Criterion Evidence
    alone, so calling it records the claim and returns an acknowledgement.

    With runtime evidence and a claim record bound, the tool enforces the
    evidence check itself: a claim naming an unbacked test is returned with
    a tool result naming every failing entry (C-IMP-1); the return is not a
    progress event. After ``MAX_CLAIM_RETURNS`` returns the next call is
    force-accepted. Without evidence or a record (unit-test seam) it keeps
    the M1 behaviour of recording unconditionally.
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
    _claim_record: ClaimRecord | None = None
    _criterion_evidence: CriterionEvidence | None = None

    def __init__(
        self,
        criteria_ids: tuple[str, ...],
        claim_sink: dict[str, object],
        *,
        criterion_evidence: CriterionEvidence | None = None,
        claim_record: ClaimRecord | None = None,
    ) -> None:
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
        object.__setattr__(self, "_criterion_evidence", criterion_evidence)
        object.__setattr__(self, "_claim_record", claim_record)

    def _run(self, **kwargs: object) -> str:
        sink = object.__getattribute__(self, "_claim_sink")
        assert isinstance(sink, dict)
        record = object.__getattribute__(self, "_claim_record")
        evidence = object.__getattribute__(self, "_criterion_evidence")
        criteria_ids: tuple[str, ...] = object.__getattribute__(self, "_criteria_ids")
        if record is None or evidence is None:
            sink.clear()
            sink.update(kwargs)
            return "completion recorded"
        assert isinstance(record, ClaimRecord)
        claim = normalize_claim(kwargs, criteria_ids)
        failures = find_evidence_failures(claim, evidence)
        if not failures:
            record.accepted_raw = dict(kwargs)
            record.force_accepted = False
            sink.clear()
            sink.update(kwargs)
            return "completion recorded"
        if record.returns < MAX_CLAIM_RETURNS:
            message = format_claim_return(failures, returns_so_far=record.returns)
            record.returns += 1
            return message
        record.returns += 1
        record.accepted_raw = dict(kwargs)
        record.force_accepted = True
        sink.clear()
        sink.update(kwargs)
        return (
            "completion force-accepted after "
            f"{MAX_CLAIM_RETURNS} returns; the claim is recorded as "
            "implemented but unverified where a candidate diff exists."
        )

    async def _arun(self, **kwargs: object) -> str:
        return self._run(**kwargs)


def build_report_completion_tool(
    criteria_ids: tuple[str, ...],
    claim_sink: dict[str, object] | None = None,
    *,
    criterion_evidence: CriterionEvidence | None = None,
    claim_record: ClaimRecord | None = None,
) -> ReportCompletionTool:
    """Build the per-Attempt ``report_completion`` tool for ``criteria_ids``."""
    return ReportCompletionTool(
        criteria_ids,
        claim_sink if claim_sink is not None else {},
        criterion_evidence=criterion_evidence,
        claim_record=claim_record,
    )


MAX_STEP_JUSTIFICATION_CHARS = 2000
"""Bound on a ``report_step`` justification (contract §8, W-STP-5).

The justification travels into the next step's Continuation Snapshot as a
bounded field, so an unbounded model string cannot blow the 32 KiB snapshot
cap. Longer input is truncated, never refused: refusing would leave the step
without an exit.
"""

REPORT_STEP_DONE = "done"
"""``report_step`` status that continues the Workflow Step list (contract §8)."""

REPORT_STEP_CANNOT_CONTINUE = "cannot-continue"
"""``report_step`` status that stops the list as ``completion not established`` (contract §8, W-STP-1)."""

_STEP_JUSTIFICATION_TRUNCATION_MARKER = "...[truncated]"


@dataclass
class StepReport:
    """A non-final step's exit, recorded by ``report_step`` (contract §8).

    ``status`` is ``done`` or ``cannot-continue``; ``justification`` is the
    model's bounded account of the step, carried into the next step's
    Continuation Snapshot (W-STP-5).
    """

    status: str
    justification: str


@dataclass
class StepReportRecord:
    """A non-final work node's exit record (contract §8).

    The record belongs to the work node the same way ``ClaimRecord`` belongs
    to the final one: a fresh step subgraph opens with a fresh record, so the
    Continuation Snapshot carries work facts and never runtime policy state.
    """

    report: StepReport | None = None


def report_step_schema() -> dict[str, Any]:
    """The JSON schema for the ``report_step`` exit tool.

    One required ``status`` (``done`` or ``cannot-continue``) and one
    required ``justification`` string, bounded in size.
    """
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [REPORT_STEP_DONE, REPORT_STEP_CANNOT_CONTINUE],
            },
            "justification": {
                "type": "string",
                "maxLength": MAX_STEP_JUSTIFICATION_CHARS,
            },
        },
        "required": ["status", "justification"],
        "additionalProperties": False,
    }


class ReportStepTool(BaseTool):
    """The non-final step's exit tool (contract §8, W-STP-2).

    Offered by position: in every non-final Workflow Step, and never in the
    final one. Calling it records the step's exit in the work node's
    ``StepReportRecord`` and ends the step from the tools node, the mirror of
    how ``report_completion`` ends the final step. The justification is
    truncated to ``MAX_STEP_JUSTIFICATION_CHARS``; a status outside the enum
    is a tool error the model can react to, never an exit.
    """

    name: str = "report_step"
    description: str = (
        "End this non-final Workflow Step. Status is 'done' to continue to "
        "the next step or 'cannot-continue' to stop the step list; "
        "justification is a bounded account of the step, carried into the "
        "next step's Continuation Snapshot."
    )
    args_schema: Any = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [REPORT_STEP_DONE, REPORT_STEP_CANNOT_CONTINUE],
            },
            "justification": {
                "type": "string",
                "maxLength": MAX_STEP_JUSTIFICATION_CHARS,
            },
        },
        "required": ["status", "justification"],
        "additionalProperties": False,
    }

    _report_record: StepReportRecord | None = None

    def __init__(self, report_record: StepReportRecord | None) -> None:
        super().__init__(
            args_schema=report_step_schema(),
            description=(
                "End this non-final Workflow Step. Status is 'done' to "
                "continue to the next step or 'cannot-continue' to stop the "
                "step list; justification is a bounded account of the step, "
                "carried into the next step's Continuation Snapshot."
            ),
        )
        object.__setattr__(self, "_report_record", report_record)

    def _run(self, **kwargs: object) -> str:
        record = object.__getattribute__(self, "_report_record")
        status = kwargs.get("status")
        justification = kwargs.get("justification", "")
        if status not in (REPORT_STEP_DONE, REPORT_STEP_CANNOT_CONTINUE):
            return (
                "Error: report_step status must be "
                f"'{REPORT_STEP_DONE}' or '{REPORT_STEP_CANNOT_CONTINUE}', "
                f"got {status!r}"
            )
        assert isinstance(status, str)
        if not isinstance(justification, str):
            return f"Error: report_step justification must be a string, got {type(justification).__name__}"
        if len(justification) > MAX_STEP_JUSTIFICATION_CHARS:
            keep = MAX_STEP_JUSTIFICATION_CHARS - len(_STEP_JUSTIFICATION_TRUNCATION_MARKER)
            justification = justification[:keep] + _STEP_JUSTIFICATION_TRUNCATION_MARKER
        if record is not None:
            assert isinstance(record, StepReportRecord)
            record.report = StepReport(status=status, justification=justification)
        return f"step {status} recorded"

    async def _arun(self, **kwargs: object) -> str:
        return self._run(**kwargs)


def build_report_step_tool(report_record: StepReportRecord | None) -> ReportStepTool:
    """Build the ``report_step`` exit tool bound to ``report_record``."""
    return ReportStepTool(report_record)
