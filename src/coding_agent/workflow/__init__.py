"""The Workflow Configuration: schema, strict parser, semantics and pinning.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table),
§2.2 (strict parsing), §2.3 (trust and paths), §2.4 (commands and validation)
and §2.5 (Effective Configuration and pinning) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-5, W-CFG-6, W-CFG-7,
W-CFG-8, W-CFG-9, W-CFG-10, W-CFG-11, W-CFG-12, C-CFG-1, C-CFG-2, C-CFG-3,
W-CFG-13 (``agent config validate`` is static and offline).

``schema`` is the typed shape and ``parser`` the strict structural loader:
``agent config validate`` refuses anything structurally wrong, naming the
YAML path. ``effective`` builds on the returned ``WorkflowConfig`` with the
semantic checks (references, the allowlist, env vars, secrets, labels) and
the pinned Effective Configuration with its digest.
"""

from coding_agent.workflow.effective import (
    DEFAULT_COST_USD,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_READY_FOR_HUMAN,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    DEFAULT_TOKENS,
    DEFAULT_TOOL_CALLS,
    DEFAULT_WALL_CLOCK_MINUTES,
    TOP_LEVEL_SKILL_ALLOWLIST,
    EffectiveConfiguration,
    EffectiveLimits,
    EffectiveModel,
    EffectivePromptFile,
    EffectiveStep,
    EffectiveTarget,
    EffectiveWorker,
    EffectiveWorkflow,
    resolve_effective_configuration,
    validate_workflow_semantics,
)
from coding_agent.workflow.parser import WorkflowConfigError, load_workflow_config
from coding_agent.workflow.schema import (
    LimitsDecl,
    ModelDecl,
    SelectorDecl,
    StepDecl,
    TargetDecl,
    TargetLabels,
    TokenPrices,
    WorkerDecl,
    WorkflowConfig,
    WorkflowDecl,
)

__all__ = [
    "DEFAULT_COST_USD",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_READY_FOR_HUMAN",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "DEFAULT_TOKENS",
    "DEFAULT_TOOL_CALLS",
    "DEFAULT_WALL_CLOCK_MINUTES",
    "TOP_LEVEL_SKILL_ALLOWLIST",
    "EffectiveConfiguration",
    "EffectiveLimits",
    "EffectiveModel",
    "EffectivePromptFile",
    "EffectiveStep",
    "EffectiveTarget",
    "EffectiveWorker",
    "EffectiveWorkflow",
    "LimitsDecl",
    "ModelDecl",
    "SelectorDecl",
    "StepDecl",
    "TargetDecl",
    "TargetLabels",
    "TokenPrices",
    "WorkerDecl",
    "WorkflowConfig",
    "WorkflowConfigError",
    "WorkflowDecl",
    "load_workflow_config",
    "resolve_effective_configuration",
    "validate_workflow_semantics",
]
