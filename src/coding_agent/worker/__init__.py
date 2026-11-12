"""The Worker: intake, Selection-and-Claim and the poll loop.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (the
Worker's lifecycle: startup in order, then the poll loop), §5 (Selection),
§6 (the Claim) and §16 (recovery and halts: unfinished Attempts settle
before the first selection, legacy unfinished Attempts refuse startup).

Matrix row ids: REC-11, OPC-8, OP-15, OP-16.
"""

from coding_agent.worker.run import (
    CREDENTIAL_REASON,
    HALT_PIN_VIOLATION,
    INFRASTRUCTURE_PREFIX,
    MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES,
    WorkerLoopConfig,
    is_credential_reason,
    is_infrastructure_reason,
    is_pin_violation_reason,
    run_worker_loop,
)

__all__ = [
    "CREDENTIAL_REASON",
    "HALT_PIN_VIOLATION",
    "INFRASTRUCTURE_PREFIX",
    "MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES",
    "WorkerLoopConfig",
    "is_credential_reason",
    "is_infrastructure_reason",
    "is_pin_violation_reason",
    "run_worker_loop",
]
