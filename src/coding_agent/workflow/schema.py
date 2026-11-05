"""Typed Workflow Configuration schema objects.

Contract: ``docs/contract/mvp-runtime-contract.md`` §2.1 (the schema table),
§2.2 (strict parsing), §2.3 (trust and paths), §2.4 (commands and validation),
§2.5 (Effective Configuration and pinning) and
[ADR 0016](../docs/adr/0016-a-workflow-fills-only-the-implementation-stage.md)
(a Workflow fills only the implementation stage).

MVP row ids: W-CFG-1, W-CFG-2, W-CFG-3, W-CFG-4, W-CFG-7, W-CFG-8, C-CFG-2,
W-CFG-13 (validate half).

These dataclasses are the typed shape the strict parser
(``coding_agent.workflow.parser``) returns and the semantic checks of the next
ticket build on: references, the skill allowlist, environment variables,
secrets, labels and the Effective Configuration. Frozen so that a value, once
loaded, is the pin — a later edit to the YAML cannot reach back and change an
open Attempt's own configuration (§2.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TokenPrices:
    """A Pinned Model's per-bucket prices in USD per million tokens (§2.1)."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class ModelDecl:
    """One ``models.<id>`` entry: a Pinned Model (§2.1)."""

    id: str
    provider: str
    model: str
    endpoint: str
    effort: str
    api_key_env: str
    prices: TokenPrices


@dataclass(frozen=True)
class TargetLabels:
    """``target.labels`` (§2.1): only the ``ready-for-human`` name is configurable."""

    ready_for_human: str | None = None


@dataclass(frozen=True)
class TargetDecl:
    """The ``target`` block: which repository, branch and credential (§2.1)."""

    kind: str
    repository: str
    base_branch: str
    token_env: str
    labels: TargetLabels = field(default_factory=TargetLabels)


@dataclass(frozen=True)
class WorkerDecl:
    """The ``worker`` block (§2.1). Absent fields take the contract defaults."""

    poll_interval_seconds: int | float | None = None
    shutdown_grace_seconds: int | float | None = None


@dataclass(frozen=True)
class SelectorDecl:
    """A Workflow's Selection Label plus its narrowing filters (§2.1, §4)."""

    label: str
    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class LimitsDecl:
    """A Workflow's ceilings (§2.1). Absent fields take the contract defaults."""

    wall_clock_minutes: int | float | None = None
    cost_usd: int | float | None = None
    tokens: int | float | None = None
    tool_calls: int | float | None = None
    max_parallel_subagents: int | None = None


@dataclass(frozen=True)
class StepDecl:
    """One ordered Workflow Step: ``id`` plus exactly one of ``skill`` or
    ``prompt_file`` (§2.1, §2.2)."""

    id: str
    skill: str | None = None
    prompt_file: str | None = None


@dataclass(frozen=True)
class WorkflowDecl:
    """One ``workflows.<id>`` entry (§2.1)."""

    id: str
    model: str
    selector: SelectorDecl
    steps: tuple[StepDecl, ...]
    limits: LimitsDecl | None = None


@dataclass(frozen=True)
class WorkflowConfig:
    """A validated Workflow Configuration file (§2.1–§2.4).

    This is the structural shape only: ids match ``[a-z0-9-]+``, ``steps`` is
    non-empty, each step carries exactly one of ``skill``/``prompt_file``,
    and every ``prompt_file`` exists inside the YAML directory. Reference,
    allowlist, environment, secret and label checks are the next ticket's.
    """

    version: int
    target: TargetDecl
    models: dict[str, ModelDecl]
    workflows: dict[str, WorkflowDecl]
    source: Path
    worker: WorkerDecl | None = None
