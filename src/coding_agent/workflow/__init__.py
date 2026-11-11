"""The Workflow Configuration: schema, strict parser, semantics and pinning.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table),
§2.2 (strict parsing), §2.3 (trust and paths), §2.4 (commands and validation)
and §2.5 (Effective Configuration and pinning) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-5, W-CFG-6, W-CFG-7,
W-CFG-8, W-CFG-9, W-CFG-10, W-CFG-11, W-CFG-12, C-CFG-1, C-CFG-2, C-CFG-3,
C-CFG-4 (``limits.max_parallel_subagents``),
W-CFG-13 (``agent config validate`` is static and offline), W-PIN-2 (the
Effective Configuration record and its digest).

``schema`` is the typed shape and ``parser`` the strict structural loader:
``agent config validate`` refuses anything structurally wrong, naming the
YAML path. ``effective`` builds on the returned ``WorkflowConfig`` with the
semantic checks (references, the allowlist, env vars, secrets, labels) and
the pinned Effective Configuration with its digest.

``v2_schema`` is the typed shape of the ``version: 2`` step-workflow
configuration (contract ``docs/contract/step-workflow-runtime-contract.md``
§2) and ``v2_validate`` its static validator (§3), reached from
``agent config validate`` but not from ``agent run`` yet (S1).
"""

from coding_agent.workflow.effective import (
    DEFAULT_COST_USD,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
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
    effective_configuration_record,
    resolve_effective_configuration,
    validate_workflow_semantics,
)
from coding_agent.workflow.parser import (
    WorkflowConfigError,
    load_workflow_config,
    strict_load_yaml,
)
from coding_agent.workflow.v2_schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    END_NODE_PREFIX,
    FIELD_TYPES,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    REQUIRABLE,
    RUNTIME_ONLY_OUTCOMES,
    TOOL_GROUPS,
    YAML_ENDS,
    V2Agent,
    V2Ceiling,
    V2Config,
    V2EnumType,
    V2FieldType,
    V2Input,
    V2InputRef,
    V2Instruction,
    V2ListType,
    V2ModelDecl,
    V2OperationSpec,
    V2Prices,
    V2RouteTarget,
    V2Step,
    V2Target,
    V2Worker,
    V2Workflow,
)
from coding_agent.workflow.v2_validate import load_v2_config, validate_v2_file
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
    "ALL_OUTCOMES",
    "ALWAYS_PRESENT_TOOLS",
    "CONDITIONAL_RESULTS",
    "DEFAULT_COST_USD",
    "DEFAULT_MAX_PARALLEL_SUBAGENTS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_READY_FOR_HUMAN",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "DEFAULT_TOKENS",
    "DEFAULT_TOOL_CALLS",
    "DEFAULT_WALL_CLOCK_MINUTES",
    "END_NODE_PREFIX",
    "FIELD_TYPES",
    "METERS",
    "OPERATIONS",
    "RESERVED_RESULTS",
    "REQUIRABLE",
    "RUNTIME_ONLY_OUTCOMES",
    "TOOL_GROUPS",
    "TOP_LEVEL_SKILL_ALLOWLIST",
    "YAML_ENDS",
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
    "V2Agent",
    "V2Ceiling",
    "V2Config",
    "V2EnumType",
    "V2FieldType",
    "V2Input",
    "V2InputRef",
    "V2Instruction",
    "V2ListType",
    "V2ModelDecl",
    "V2OperationSpec",
    "V2Prices",
    "V2RouteTarget",
    "V2Step",
    "V2Target",
    "V2Worker",
    "V2Workflow",
    "WorkerDecl",
    "WorkflowConfig",
    "WorkflowConfigError",
    "WorkflowDecl",
    "effective_configuration_record",
    "load_v2_config",
    "load_workflow_config",
    "resolve_effective_configuration",
    "strict_load_yaml",
    "validate_v2_file",
    "validate_workflow_semantics",
]
