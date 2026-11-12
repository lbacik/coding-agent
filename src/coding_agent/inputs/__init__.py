"""Step-workflow Workflow Inputs and the Claim (contract §5–§7, §16.5).

Slice S6 of ``docs/plan/step-workflow-implementation-plan.md``. Since the
S10 cutover these modules are the live intake path, reachable from
``agent run``; the MVP runtime is gone.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection: one input type per Workflow, the Selection Label,
authorisation by the ``labeled`` event under the detected identity mode,
mandatory guards, YAML filters, drafts, ``skip-open-agent-pr``, ordering),
§6 (Claim and Attempt identity: global exclusivity, the assignment plus a
Claim Comment, the Claim sequence, ``<workflow>#<N>/<n>`` identity,
``n`` rebuilt from markers after a ledger loss), §7 (pinned input state,
Gates, Drift and withdrawal) and §16.5 (ledger loss).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.
"""
