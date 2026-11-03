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
``uncovered`` criterion is accepted, never returned; a value that is neither
a non-empty list of test identifiers nor the ``uncovered`` marker
(``["uncovered"]``, or the bare string) is malformed,
never ``uncovered``: the call is returned naming each malformed criterion
and the expected shape, and a malformed return counts against the same
two-return budget; a claim made with a candidate diff and no completed
review in the Attempt is returned once, naming the missing review, and
accepted when made again without one) and §1 (the model's toolset contains
no GitHub mutation; there is no shell tool and file tools cannot reach the
runtime-owned ``{evidence_dir}``).

A string that parses as a JSON list of strings is still a string, not a
list: it is returned as malformed, never coerced and never read as
``uncovered``. Malformed returns share the evidence budget (two returns,
then force-accept) so a misshapen claim cannot loop past the budget. The
review return is separate: it spends none of that budget.

MVP row ids: L2-2, L3-IMP-10, L3-IMP-20, C-IMP-1, L3-IMP-28, L3-IMP-29,
W-STP-1, W-STP-2, W-STP-5, C-STP-1, P-DEL-3.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.tools import BaseTool

from coding_agent.validate.junit import JUnitResult

UNCOVERED = "uncovered"
"""The explicit marker a claim uses for a criterion with no covering test.

The schema asks for it as a one-element list, ``["uncovered"]``: every
criterion's value is then a list of strings, with no ``anyOf``. With the
``anyOf`` of a list and a string, Anthropic models sent every list as a
JSON-encoded string (#134). The bare string ``"uncovered"`` is still
accepted as the same marker.
"""

MAX_LISTED_RECORDED_IDS = 50
"""How many recorded test ids an evidence return lists at most (contract §9)."""

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


@dataclass(frozen=True)
class MalformedClaimEntry:
    """One criterion whose claimed value matches neither alternative (§9).

    ``received`` describes what the model sent (its type and a truncated
    ``repr``), so the return can show why the value is not a covering list.
    """

    criterion_id: str
    received: str


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
    review_status: str | None = None
    """The Attempt's review status (§10) when the claim was accepted.

    ``None`` where no review state is bound (the unit-test seam) or no
    claim was accepted yet. MVP row ids: P-DEL-3.
    """


class ClaimReviewGate(Protocol):
    """The Attempt-wide review state the claim's review check reads (§9, §10).

    ``ReviewSnapshotState`` satisfies it. ``claim_review_returned`` is
    written by the check, so the review return happens once per Attempt
    even when a handoff gives the work node a fresh ``ClaimRecord``.
    """

    claim_review_returned: bool

    @property
    def status(self) -> str: ...

    @property
    def review_gate_met(self) -> bool: ...


def _is_uncovered_marker(value: object) -> bool:
    """Whether ``value`` is the explicit ``uncovered`` marker (§9).

    ``["uncovered"]``, the form the schema asks for, or the bare string.
    """
    if isinstance(value, str):
        return value == UNCOVERED
    return isinstance(value, (list, tuple)) and list(value) == [UNCOVERED]


def _is_covering_list(value: object) -> bool:
    """Whether ``value`` is a non-empty list of test identifiers (§9).

    Every item is a non-empty string, and none is the ``uncovered`` marker:
    a list mixing the marker with identifiers is malformed, never read as
    either alternative.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return False
    return all(isinstance(item, str) and item and item != UNCOVERED for item in value)


def _describe_received(value: object) -> str:
    """Describe a malformed claim value by type and truncated ``repr``."""
    text = repr(value)
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{type(value).__name__} {text}"


def find_malformed_claim_entries(
    raw: Mapping[str, object] | None,
    expected_ids: tuple[str, ...],
) -> list[MalformedClaimEntry]:
    """List every criterion whose value matches neither claim alternative.

    A criterion is well-formed where its value is the ``uncovered`` marker
    (``["uncovered"]`` or the bare string) or a non-empty list of test
    identifiers. Anything else — including a
    string that parses as a JSON list of strings, which is still a string
    and is never coerced — is malformed, never ``uncovered`` (contract §9).
    """
    malformed: list[MalformedClaimEntry] = []
    source: Mapping[str, object] = raw or {}
    for criterion_id in expected_ids:
        value = source.get(criterion_id, UNCOVERED)
        if _is_uncovered_marker(value) or _is_covering_list(value):
            continue
        malformed.append(
            MalformedClaimEntry(
                criterion_id=criterion_id, received=_describe_received(value)
            )
        )
    return malformed


def normalize_claim(
    raw: Mapping[str, object] | None,
    expected_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...] | None]:
    """Normalize a ``report_completion`` claim to per-criterion coverage.

    Returns one entry per expected ``AC-n`` id: a tuple of covering test
    identifiers, ``None`` for an explicit ``uncovered`` marker or a missing
    entry, or an empty tuple for a malformed value. ``None`` is kept only
    for an explicit ``uncovered`` (or a missing entry, which the schema
    prevents); a malformed value is reported separately through
    :func:`find_malformed_claim_entries` and is never accepted as
    ``uncovered`` (contract §9). The empty-tuple fallback keeps decision
    time safe where a malformed claim reaches it:
    ``decide_implementation_outcome`` treats it as ``criteria-uncovered``,
    never as a verified outcome.
    """
    claim: dict[str, tuple[str, ...] | None] = {}
    source: Mapping[str, object] = raw or {}
    for criterion_id in expected_ids:
        value = source.get(criterion_id, UNCOVERED)
        if _is_uncovered_marker(value):
            claim[criterion_id] = None
            continue
        if _is_covering_list(value):
            assert isinstance(value, (list, tuple))
            claim[criterion_id] = tuple(
                item for item in value if isinstance(item, str) and item
            )
            continue
        claim[criterion_id] = ()
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


def format_claim_return(
    failures: list[ClaimFailure],
    *,
    returns_so_far: int,
    recorded_ids: frozenset[str] = frozenset(),
) -> str:
    """Render the tool result returning a claim (contract §9, C-IMP-1).

    The result names every failing entry so the model can react: one line
    per ``(criterion, test)`` pair with its reason. The return count is
    included so the model sees the budget (two returns, then force-accept).
    Where an entry is absent from Criterion Evidence, the result also lists
    the recorded ids (at most ``MAX_LISTED_RECORDED_IDS``), so the model can
    name a test in the form the evidence records it.
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
        f'criterion ["{UNCOVERED}"] where it has no covering test.'
    )
    if any(failure.reason == "absent from Criterion Evidence" for failure in failures):
        listed = sorted(recorded_ids)[:MAX_LISTED_RECORDED_IDS]
        if listed:
            more = len(recorded_ids) - len(listed)
            suffix = f" (and {more} more)" if more > 0 else ""
            lines.append(
                "Name tests exactly as Criterion Evidence records them. "
                f"Recorded test ids{suffix}:"
            )
            lines.extend(f"- {test_id}" for test_id in listed)
        else:
            lines.append("Criterion Evidence has no recorded test yet.")
    return "\n".join(lines)


def format_review_return(status: str) -> str:
    """Render the tool result returning a claim for a missing review (§9, P-DEL-3).

    The result names the missing review and the tool that runs it, says
    the return happens once, and that a repeated claim without a review is
    accepted with the review gate unmet.
    """
    return (
        "Completion claim returned (once): no completed review in this "
        f"Attempt (review status: {status}). A completed review needs both "
        "reviewer seats, Standards and Spec, to make a Review Report. Call "
        "the activate_code_review tool to run it, then call "
        "report_completion again. A claim made again without a review is "
        "accepted, with the review gate unmet."
    )


def format_malformed_claim_return(
    entries: list[MalformedClaimEntry], *, returns_so_far: int
) -> str:
    """Render the tool result returning a malformed claim (contract §9).

    The result names every malformed criterion, what was received, and the
    expected shape: a non-empty list of test identifier strings, or
    ``["uncovered"]``. A JSON-encoded string is a string, not a list: it must be
    resent as an actual list. The return count is included so the model sees
    the shared budget (two returns, then force-accept).
    """
    lines = [
        f"Completion claim returned ({returns_so_far + 1}/{MAX_CLAIM_RETURNS + 1} "
        "before force-accept): the following criteria have a malformed value "
        "(expected a non-empty list of test identifier strings, or "
        f'["{UNCOVERED}"]):'
    ]
    for entry in entries:
        lines.append(f"- {entry.criterion_id}: got {entry.received}")
    lines.append(
        "Resend each criterion as a list of test identifier strings "
        "(a JSON-encoded string is not a list), "
        f'or ["{UNCOVERED}"] where it has no covering test.'
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
      for an explicitly uncovered, otherwise uncovered, or malformed
      (empty, never verified) criterion, ``claims-unverified`` for a
      force-accepted claim with no candidate diff, or
      ``no-completion-evidence`` where no claim was made.

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
    criterion, each a non-empty list of strings: the covering test
    identifiers, or ``["uncovered"]``. The schema has no ``anyOf``: with a
    list-or-string union, Anthropic models sent the lists as JSON-encoded
    strings (#134).
    """
    properties: dict[str, Any] = {}
    for criterion_id in criteria_ids:
        properties[criterion_id] = {
            "description": (
                f"Test identifiers covering {criterion_id}, "
                f'or ["{UNCOVERED}"] where it has none.'
            ),
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
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
    claim checks itself: a criterion whose value is neither a covering list
    nor the ``uncovered`` marker is returned as malformed, naming each
    malformed criterion and the expected shape (a JSON-encoded string is a
    string, not a list); a claim naming an unbacked test is returned with
    a tool result naming every failing entry (C-IMP-1); each return is not
    a progress event. Malformed and evidence returns share the same
    ``MAX_CLAIM_RETURNS`` budget: after that many returns the next call is
    force-accepted. Without evidence or a record (unit-test seam) it keeps
    the M1 behaviour of recording unconditionally.

    With ``review_gate`` also bound, a claim the evidence check accepts
    without force is then checked for review (§9 claim check 2, P-DEL-3):
    with a candidate diff and no completed review in the Attempt, it is
    returned once, naming the missing review. That return is not an
    evidence return, so it spends none of the two-return budget, and it is
    not a progress event. Made again without a review, the claim is
    accepted and the Publication Gate decides. A force-accepted claim is
    not returned for review: the third call is force-accepted, as §9's
    evidence check says. The review status at acceptance is recorded on
    the ``ClaimRecord``.
    """

    name: str = "report_completion"
    description: str = (
        "Declare the implementation stage finished with per-criterion "
        "coverage. For each AC-n id give the list of covering test "
        'identifiers, or ["uncovered"].'
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
    _review_gate: ClaimReviewGate | None = None
    _has_candidate_diff: Callable[[], bool] | None = None

    def __init__(
        self,
        criteria_ids: tuple[str, ...],
        claim_sink: dict[str, object],
        *,
        criterion_evidence: CriterionEvidence | None = None,
        claim_record: ClaimRecord | None = None,
        review_gate: ClaimReviewGate | None = None,
        has_candidate_diff: Callable[[], bool] | None = None,
    ) -> None:
        if review_gate is not None and has_candidate_diff is None:
            raise ValueError(
                "a review_gate needs has_candidate_diff: the review return "
                "applies only to a claim made with a candidate diff (§9)"
            )
        super().__init__(
            args_schema=report_completion_schema(criteria_ids),
            description=(
                "Declare the implementation stage finished. "
                f"Criteria: {', '.join(criteria_ids) if criteria_ids else '(none)'}. "
                "For each AC-n id give the list of covering test "
                f'identifiers, or ["{UNCOVERED}"].'
            ),
        )
        object.__setattr__(self, "_claim_sink", claim_sink)
        object.__setattr__(self, "_criteria_ids", tuple(criteria_ids))
        object.__setattr__(self, "_criterion_evidence", criterion_evidence)
        object.__setattr__(self, "_claim_record", claim_record)
        object.__setattr__(self, "_review_gate", review_gate)
        object.__setattr__(self, "_has_candidate_diff", has_candidate_diff)

    def _review_return(self) -> str | None:
        """The once-per-Attempt review return, or ``None`` to accept (§9, P-DEL-3)."""
        gate = object.__getattribute__(self, "_review_gate")
        if gate is None or gate.claim_review_returned or gate.review_gate_met:
            return None
        has_candidate_diff = object.__getattribute__(self, "_has_candidate_diff")
        assert has_candidate_diff is not None
        if not has_candidate_diff():
            return None
        gate.claim_review_returned = True
        return format_review_return(gate.status)

    def _accept(
        self, record: ClaimRecord, kwargs: dict[str, object], *, forced: bool
    ) -> None:
        sink = object.__getattribute__(self, "_claim_sink")
        assert isinstance(sink, dict)
        gate = object.__getattribute__(self, "_review_gate")
        record.accepted_raw = dict(kwargs)
        record.force_accepted = forced
        record.review_status = gate.status if gate is not None else None
        sink.clear()
        sink.update(kwargs)

    def _force_accept(self, record: ClaimRecord, kwargs: dict[str, object]) -> str:
        """Record a force-accepted claim after the return budget ran out (§9).

        Shared by the malformed and evidence return paths: both count
        against the same ``MAX_CLAIM_RETURNS`` budget, so the third call is
        recorded as force-accepted either way, never as verified, and with
        the review status at acceptance.
        """
        record.returns += 1
        self._accept(record, dict(kwargs), forced=True)
        return (
            "completion force-accepted after "
            f"{MAX_CLAIM_RETURNS} returns; the claim is recorded as "
            "implemented but unverified where a candidate diff exists."
        )

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
        malformed = find_malformed_claim_entries(kwargs, criteria_ids)
        if malformed:
            if record.returns < MAX_CLAIM_RETURNS:
                message = format_malformed_claim_return(
                    malformed, returns_so_far=record.returns
                )
                record.returns += 1
                return message
            return self._force_accept(record, kwargs)
        claim = normalize_claim(kwargs, criteria_ids)
        failures = find_evidence_failures(claim, evidence)
        if not failures:
            review_return = self._review_return()
            if review_return is not None:
                return review_return
            self._accept(record, dict(kwargs), forced=False)
            return "completion recorded"
        if record.returns < MAX_CLAIM_RETURNS:
            message = format_claim_return(
                failures,
                returns_so_far=record.returns,
                recorded_ids=evidence.executed_ids(),
            )
            record.returns += 1
            return message
        return self._force_accept(record, kwargs)

    async def _arun(self, **kwargs: object) -> str:
        return self._run(**kwargs)


def build_report_completion_tool(
    criteria_ids: tuple[str, ...],
    claim_sink: dict[str, object] | None = None,
    *,
    criterion_evidence: CriterionEvidence | None = None,
    claim_record: ClaimRecord | None = None,
    review_gate: ClaimReviewGate | None = None,
    has_candidate_diff: Callable[[], bool] | None = None,
) -> ReportCompletionTool:
    """Build the per-Attempt ``report_completion`` tool for ``criteria_ids``."""
    return ReportCompletionTool(
        criteria_ids,
        claim_sink if claim_sink is not None else {},
        criterion_evidence=criterion_evidence,
        claim_record=claim_record,
        review_gate=review_gate,
        has_candidate_diff=has_candidate_diff,
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
