"""The Workflow Configuration: schema, strict loader and static validation.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the
``version: 2`` configuration file), §2.7 (strict parsing), §2.8 (trust and
paths) and §3 (static validation).

Matrix row id: CFG-38.

``parser`` is the shared strict structural loader: ``agent config
validate`` refuses anything structurally wrong, naming the YAML path.
``v2_schema`` is the typed shape of the ``version: 2`` step-workflow
configuration and ``v2_validate`` its static validator. The ``version: 1``
MVP configuration is gone at the S10 cutover (§16.4).
"""

from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.v2_schema import (
    ALL_OUTCOMES,
    ALWAYS_PRESENT_TOOLS,
    CONDITIONAL_RESULTS,
    DEFAULT_MAX_PARALLEL_SUBAGENTS,
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

__all__ = [
    "ALL_OUTCOMES",
    "ALWAYS_PRESENT_TOOLS",
    "CONDITIONAL_RESULTS",
    "DEFAULT_MAX_PARALLEL_SUBAGENTS",
    "END_NODE_PREFIX",
    "FIELD_TYPES",
    "METERS",
    "OPERATIONS",
    "RESERVED_RESULTS",
    "REQUIRABLE",
    "RUNTIME_ONLY_OUTCOMES",
    "TOOL_GROUPS",
    "YAML_ENDS",
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
    "WorkflowConfigError",
    "load_v2_config",
    "strict_load_yaml",
    "validate_v2_file",
]
