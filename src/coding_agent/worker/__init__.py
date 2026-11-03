"""The Worker: intake, Selection, the Claim and the poll loop.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (the poll loop and
intake: while the ledger holds an Attempt that is not ``finished`` or
``released`` the Worker selects no new work), §4 (Selection) and §5 (the
Claim).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-4, L3-ERR-8.
"""

from coding_agent.worker.run import (
    CREDENTIAL_REASON,
    INFRASTRUCTURE_PREFIX,
    MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES,
    WorkerLoopConfig,
    is_credential_reason,
    is_infrastructure_reason,
    run_worker_loop,
)

__all__ = [
    "CREDENTIAL_REASON",
    "INFRASTRUCTURE_PREFIX",
    "MAX_CONSECUTIVE_INFRASTRUCTURE_FAILURES",
    "WorkerLoopConfig",
    "is_credential_reason",
    "is_infrastructure_reason",
    "run_worker_loop",
]
