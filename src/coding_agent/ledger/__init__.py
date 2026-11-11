"""The Run Ledger: the only durable state a Worker keeps.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (the review
snapshot is the first external write), §13 (the write sequence keyed by
``(attempt_id, write_kind)`` with ``intent`` or ``receipt`` status), §17
("Storage": one SQLite file on the state volume, WAL mode with
``synchronous=FULL``, tables created with ``CREATE TABLE IF NOT EXISTS``,
no schema version) and §14 ("Recording before publishing": every Terminal
Outcome is recorded before its first write, per ADR 0017).

MVP row ids: C-GRF-1 (the graph's durable side), REC-OUT-1, REC-OUT-2,
L3-DEL-19, L3-DEL-20, L3-ERR-9, L3-ERR-10.

``RunLedger`` moved here from ``coding_agent.runtime.progress`` in M1; that
module was removed in M2 when the step subgraph took over the progress,
soft-stall and verification-reserve policy (``runtime.step_progress``).
"""

from coding_agent.ledger.offline import open_offline_ledger
from coding_agent.ledger.store import (
    ADMISSION_HOLD_CHARGED,
    ADMISSION_HOLD_HELD,
    ADMISSION_HOLD_SETTLED,
    CLAIM_CONFIRMED,
    EFFECT_STATUS_INTENT,
    EFFECT_STATUS_RECEIPT,
    FINISHED,
    LEDGER_SCHEMA_VERSION,
    OPEN,
    OUTCOME_RECORDED,
    RELEASED,
    STEP_EFFECT_STATUS_INTENT,
    STEP_EFFECT_STATUS_NOT_PERFORMED,
    STEP_EFFECT_STATUS_RECEIPT,
    STEP_EXECUTION_ENDED,
    STEP_EXECUTION_INTERRUPTED,
    STEP_EXECUTION_OPEN,
    STEP_TRANSITION_END,
    STEP_TRANSITION_REFUSED,
    STEP_TRANSITION_ROUTE,
    AdmissionHoldRecord,
    AttemptRecord,
    BudgetSnapshot,
    EffectRecord,
    LedgerRecord,
    OutcomeRecord,
    RunLedger,
    StepEffectRecord,
    StepExecutionRecord,
    StepTransitionRecord,
    digest_effect_payload,
    step_effect_key,
    step_effect_marker,
)

__all__ = [
    "ADMISSION_HOLD_CHARGED",
    "ADMISSION_HOLD_HELD",
    "ADMISSION_HOLD_SETTLED",
    "CLAIM_CONFIRMED",
    "EFFECT_STATUS_INTENT",
    "EFFECT_STATUS_RECEIPT",
    "FINISHED",
    "LEDGER_SCHEMA_VERSION",
    "OPEN",
    "OUTCOME_RECORDED",
    "RELEASED",
    "STEP_EFFECT_STATUS_INTENT",
    "STEP_EFFECT_STATUS_NOT_PERFORMED",
    "STEP_EFFECT_STATUS_RECEIPT",
    "STEP_EXECUTION_ENDED",
    "STEP_EXECUTION_INTERRUPTED",
    "STEP_EXECUTION_OPEN",
    "STEP_TRANSITION_END",
    "STEP_TRANSITION_REFUSED",
    "STEP_TRANSITION_ROUTE",
    "AdmissionHoldRecord",
    "AttemptRecord",
    "BudgetSnapshot",
    "EffectRecord",
    "LedgerRecord",
    "OutcomeRecord",
    "RunLedger",
    "StepEffectRecord",
    "StepExecutionRecord",
    "StepTransitionRecord",
    "digest_effect_payload",
    "open_offline_ledger",
    "step_effect_key",
    "step_effect_marker",
]
