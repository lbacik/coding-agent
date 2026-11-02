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

from coding_agent.ledger.store import (
    CLAIM_CONFIRMED,
    EFFECT_STATUS_INTENT,
    EFFECT_STATUS_RECEIPT,
    FINISHED,
    OPEN,
    OUTCOME_RECORDED,
    RELEASED,
    AttemptRecord,
    BudgetSnapshot,
    EffectRecord,
    LedgerRecord,
    OutcomeRecord,
    RunLedger,
    digest_effect_payload,
)

__all__ = [
    "CLAIM_CONFIRMED",
    "EFFECT_STATUS_INTENT",
    "EFFECT_STATUS_RECEIPT",
    "FINISHED",
    "OPEN",
    "OUTCOME_RECORDED",
    "RELEASED",
    "AttemptRecord",
    "BudgetSnapshot",
    "EffectRecord",
    "LedgerRecord",
    "OutcomeRecord",
    "RunLedger",
    "digest_effect_payload",
]
