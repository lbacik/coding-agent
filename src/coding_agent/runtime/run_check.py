"""The implementing Step's diagnostic in-loop check runs (``run_check``).

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (``run_check(name)``
runs one Validation Contract check exactly as pinned: the schema is an enum
generated per Attempt from the Validation Contract's ``checks``; under
``checks: none`` the tool is not bound; it runs the pinned command with
``{evidence_dir}`` substituted under its own per-Attempt subdirectory,
credential-free (§18), with a process-group kill on timeout, returning the
exit code and output, redacted and capped; where the check declares
``evidence`` it also returns the parsed JUnit failures) and §9 (each call
records an Execution Footprint and sits behind the Ref Guard like every
code-executing tool; its result is recorded in the Run Ledger and is an
input to nothing: not to the Completion Claim checks, not to the
Implementation Outcome, not to the Publication Gate).

An in-loop ``run_check`` result is never Validation Evidence: ``final_checks``
(§12) stays the one authoritative run, and a green in-loop check does not
make ``final_checks`` green.

MVP row ids: C-CHK-1, C-CHK-2, C-CHK-3, C-CHK-4, C-CHK-5, C-CHK-6, C-CHK-7,
C-CHK-8, C-CHK-9.
"""

from __future__ import annotations

from coding_agent.ledger.store import BudgetSnapshot, LedgerRecord, RunLedger
from coding_agent.validate.junit import JUnitResult

IN_LOOP_CHECKS_EVIDENCE_SUBDIR = "in-loop-checks"
"""Per-Attempt evidence subdirectory the ``run_check`` tool owns (§8, §9).

Parallel to the ``final-checks`` subdirectory ``final_checks`` owns and the
``targeted`` subtree ``test_targeted`` owns: ``run_check`` renders every
``{evidence_dir}`` placeholder under ``<evidence-dir>/in-loop-checks/<n>``
(one numbered invocation directory per call), so an in-loop check never
writes to ``test_targeted``'s or ``final_checks``'s evidence paths.
"""

IN_LOOP_CHECK_KIND = "in_loop_check"
"""Run Ledger event kind for one in-loop ``run_check`` result (§9).

Distinct from the ``final_checks`` Validation Evidence kind: this result is
diagnostic only and is an input to nothing.
"""


def record_in_loop_check_event(
    ledger: RunLedger,
    attempt_id: str,
    snapshot: BudgetSnapshot,
    *,
    check_name: str,
    command: str,
    exit_code: int,
    timed_out: bool,
    junit: JUnitResult | None,
) -> LedgerRecord:
    """Record one in-loop ``run_check`` result as a Run Ledger event (§9).

    The payload carries the check name, the rendered command, the exit code,
    the timeout flag and, where the check declares ``evidence`` and its report
    parsed, the JUnit summary: the executed count and the sorted failure
    identifiers. A check declaring no evidence, or whose report could not be
    read, records nulls there — the exit code and timeout flag still stand.
    """
    junit_payload: dict[str, object] | None = None
    if junit is not None:
        junit_payload = {
            "executed": junit.executed,
            "failure_ids": sorted(junit.failure_ids),
        }
    return ledger.record(
        attempt_id,
        IN_LOOP_CHECK_KIND,
        snapshot,
        detail=(
            f"in-loop check {check_name!r} exited {exit_code}"
            + (" (timed out)" if timed_out else "")
        ),
        payload={
            "name": check_name,
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "junit": junit_payload,
        },
    )
