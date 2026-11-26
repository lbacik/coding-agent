"""The Workflow Configuration: schema, static validation and JSON Schema.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the
``version: 4`` configuration file; ``version: 3`` and ``version: 2`` are
refused with migration diagnostics), §2.7 (strict parsing), §2.8 (trust and
paths) and §3 (static validation).

``configuration`` is the single entry point
(``load_workflow_configuration``): ``version: 4`` validates, anything else
is refused naming the ``version`` path. ``schema`` holds the validated
types and vocabulary, ``_checks`` the private static-validation phases and
``jsonschema`` the editor-aid JSON Schema builder. ``parser`` is the shared
strict structural loader. ``effective`` pins the Effective Configuration.
"""

from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from coding_agent.workflow.jsonschema import (
    build_workflow_json_schema,
    dump_workflow_json_schema,
)
from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
    EFFORTS,
    END_NODE_PREFIX,
    FIELD_TYPES,
    INPUT_DRAFTS,
    INPUT_TYPES,
    METERS,
    OPERATIONS,
    RESERVED_RESULTS,
    REQUIRABLE,
    RUNTIME_ONLY_OUTCOMES,
    TARGET_KINDS,
    TOOL_GROUPS,
    YAML_ENDS,
    normalize_skill_source,
    skill_membership,
    Advisor,
    Agent,
    AttemptCeiling,
    EnumType,
    FieldType,
    InputDecl,
    InputRef,
    Instruction,
    ListType,
    ModelDecl,
    OperationSpec,
    ModelPrices,
    RouteTarget,
    WorkflowStep,
    TargetRepository,
    WorkerSettings,
    Workflow,
    WorkflowConfiguration,
    SkillBundleRequirement,
)

__all__ = [
    "ALL_OUTCOMES",
    "ALWAYS_PRESENT_TOOLS",
    "CONDITIONAL_RESULTS",
    "DEFAULT_MAX_PARALLEL_SUBAGENTS",
    "EFFORTS",
    "END_NODE_PREFIX",
    "FIELD_TYPES",
    "INPUT_DRAFTS",
    "INPUT_TYPES",
    "METERS",
    "OPERATIONS",
    "RESERVED_RESULTS",
    "REQUIRABLE",
    "RUNTIME_ONLY_OUTCOMES",
    "TARGET_KINDS",
    "TOOL_GROUPS",
    "YAML_ENDS",
    "Advisor",
    "Agent",
    "AttemptCeiling",
    "EnumType",
    "FieldType",
    "InputDecl",
    "InputRef",
    "Instruction",
    "ListType",
    "ModelDecl",
    "OperationSpec",
    "ModelPrices",
    "RouteTarget",
    "WorkflowStep",
    "TargetRepository",
    "WorkerSettings",
    "Workflow",
    "WorkflowConfiguration",
    "SkillBundleRequirement",
    "WorkflowConfigError",
    "LoadedWorkflowConfiguration",
    "build_workflow_json_schema",
    "dump_workflow_json_schema",
    "load_workflow_configuration",
    "normalize_skill_source",
    "skill_membership",
    "strict_load_yaml",
]
