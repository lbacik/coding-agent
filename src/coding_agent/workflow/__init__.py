"""The Workflow Configuration: schema, strict parser and (next ticket) pinning.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2 (Workflow
Configuration) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-7, W-CFG-8, C-CFG-2,
W-CFG-13 (validate half).

This ticket's half is the schema (``schema``) and the strict structural
parser (``parser``): ``agent config validate`` checks a file offline and
refuses anything structurally wrong, naming the YAML path. The semantic
checks (references, the allowlist, env vars, secrets, labels) and the
Effective Configuration are the next ticket; they build on the returned
``WorkflowConfig``.
"""

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
]
