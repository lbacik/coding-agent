"""The Run Ledger: the only durable state a Worker keeps.

Contract: ``docs/contract/mvp-runtime-contract.md`` §17 ("Storage": one
SQLite file on the state volume, WAL mode with ``synchronous=FULL``, tables
created with ``CREATE TABLE IF NOT EXISTS``, no schema version) and §14
("Recording before publishing": every Terminal Outcome is recorded before
its first write, per ADR 0017).

MVP row ids: C-GRF-1 (the graph's durable side), REC-OUT-1, REC-OUT-2.

``RunLedger`` moved here from ``coding_agent.runtime.progress`` in M1; that
module re-exports it so previously green rows stay green.
"""

from coding_agent.ledger.store import (
    CLAIM_CONFIRMED,
    FINISHED,
    OPEN,
    OUTCOME_RECORDED,
    RELEASED,
    AttemptRecord,
    BudgetSnapshot,
    LedgerRecord,
    OutcomeRecord,
    RunLedger,
)

__all__ = [
    "CLAIM_CONFIRMED",
    "FINISHED",
    "OPEN",
    "OUTCOME_RECORDED",
    "RELEASED",
    "AttemptRecord",
    "BudgetSnapshot",
    "LedgerRecord",
    "OutcomeRecord",
    "RunLedger",
]
