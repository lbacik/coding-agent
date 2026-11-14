"""The Workflow Configuration: schema, strict loader and static validation.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the
``version: 2`` and ``version: 3`` configuration files), §2.7 (strict
parsing), §2.8 (trust and paths) and §3 (static validation).

Matrix row ids: CFG-38, SKV3-01 … SKV3-04.

``parser`` is the shared strict structural loader: ``agent config
validate`` refuses anything structurally wrong, naming the YAML path.
``v2_schema`` is the typed shape of the ``version: 2`` step-workflow
configuration and ``v2_validate`` its static validator. ``v3_schema`` adds
the configuration-wide ``skills`` declaration to that shape and
``v3_validate`` its static validator (``version: 2`` stays accepted). The
``version: 1`` MVP configuration is gone at the S10 cutover (§16.4).
"""

from coding_agent.workflow.parser import WorkflowConfigError, strict_load_yaml
from coding_agent.workflow.v2_jsonschema import build_v2_json_schema, dump_v2_json_schema
from coding_agent.workflow.v2_schema import (
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
from coding_agent.workflow.v3_jsonschema import build_v3_json_schema, dump_v3_json_schema
from coding_agent.workflow.v3_schema import (
    normalize_skill_source,
    skill_membership,
    V3Config,
    V3SkillDecl,
)
from coding_agent.workflow.v3_validate import (
    load_v3_config,
    peek_config_version,
    validate_config_file,
    validate_v3_file,
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
    "build_v2_json_schema",
    "build_v3_json_schema",
    "dump_v2_json_schema",
    "dump_v3_json_schema",
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
    "V3Config",
    "V3SkillDecl",
    "WorkflowConfigError",
    "load_v2_config",
    "load_v3_config",
    "normalize_skill_source",
    "peek_config_version",
    "skill_membership",
    "strict_load_yaml",
    "validate_config_file",
    "validate_v2_file",
    "validate_v3_file",
]
