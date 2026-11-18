"""Typed Workflow Configuration objects (`version: 3`).

`version: 2` is no longer supported — `agent config validate`
and `agent run` refuse it with a migration diagnostic (SKV3-01).


Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file,
the Workflow keys, steps, results, payload types, inputs, operation
parameters) and §8.5 (the reserved end-node prefix).

Slice S1 of ``docs/plan/step-workflow-implementation-plan.md``. These
dataclasses are the typed shape the static validator
(``coding_agent.workflow._checks``) returns. Frozen so that a value, once
loaded, is the pin — a later edit to the YAML cannot reach back and change an
open Attempt's own configuration (§2.10, pinned fully in S2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

# --- Vocabulary shared with the validator (and later slices) -----------------

RESERVED_RESULTS: tuple[str, ...] = ("error", "limit-exceeded")
"""Step Results the runtime produces; a Workflow never declares them (§2.4)."""

CONDITIONAL_RESULTS: tuple[str, ...] = ("requirement-unmet", "stale-head")
"""Catalog results a step produces only under a condition (§2.4, §3)."""

YAML_ENDS: dict[str, bool] = {
    "completed": False,
    "blocked": False,
    "unfinished": True,
    "failed": True,
}
"""YAML-selectable Workflow Ends; the value says whether a runtime-reserved
result may route directly to it (§3, §15.1)."""

RUNTIME_ONLY_OUTCOMES: tuple[str, ...] = (
    "withdrawn",
    "drift",
    "interrupted",
    "internal-failure",
)
"""Terminal Outcomes the runtime selects; never a Route target (§3, §12)."""

ALL_OUTCOMES: tuple[str, ...] = tuple(YAML_ENDS) + RUNTIME_ONLY_OUTCOMES
"""Every key `outcomes` may name (§3, §15.1). There is no `canceled`."""

TOOL_GROUPS: tuple[str, ...] = (
    "workspace-read",
    "workspace-write",
    "shell",
    "tests",
    "subagents",
)
"""Tool groups an `agent` step may declare (§9.4)."""

ALWAYS_PRESENT_TOOLS: tuple[str, ...] = (
    "report",
    "report-result",
    "activate-skill",
    "read-result",
)
"""Tools every `agent` step binds without declaration; declaring one is
refused (§9.4)."""

METERS: tuple[str, ...] = ("cost-usd", "tokens", "tool-calls", "wall-clock-minutes")
"""The four limit dimensions (§11.1)."""

FIELD_TYPES: tuple[str, ...] = ("document", "string", "integer", "boolean", "evidence-ref")
"""Scalar Step Result payload field types (§2.4)."""

TARGET_KINDS: tuple[str, ...] = ("github",)
"""Allowed `target.kind` values (§2.1)."""

EFFORTS: tuple[str, ...] = ("low", "medium", "high")
"""Allowed `models.<id>.effort` values (§2.1)."""

INPUT_TYPES: tuple[str, ...] = ("issue", "pull-request")
"""Allowed `workflows.<id>.input.type` values (§2.2, §5)."""

INPUT_DRAFTS: tuple[str, ...] = ("include", "exclude")
"""Allowed `workflows.<id>.input.drafts` values (pull-request inputs only)."""


@dataclass(frozen=True)
class V2OperationSpec:
    """One catalog Runtime Operation: its business results, allowed `with`
    keys, input-type restriction and whether it publishes a body (§10.1)."""

    business: tuple[str, ...]
    with_keys: frozenset[str]
    input_type: str | None
    needs_body: bool


OPERATIONS: dict[str, V2OperationSpec] = {
    "check-readiness": V2OperationSpec(
        business=("ready", "not-ready"),
        with_keys=frozenset(),
        input_type=None,
        needs_body=False,
    ),
    "run-validation": V2OperationSpec(
        business=("passed", "failed"),
        with_keys=frozenset(),
        input_type=None,
        needs_body=False,
    ),
    "commit-and-push": V2OperationSpec(
        business=("pushed", "no-changes"),
        with_keys=frozenset({"requires"}),
        input_type=None,
        needs_body=False,
    ),
    "open-pull-request": V2OperationSpec(
        business=("opened", "already-open"),
        with_keys=frozenset({"body", "draft", "requires"}),
        input_type="issue",
        needs_body=True,
    ),
    "publish-review": V2OperationSpec(
        business=("published", "stale-head"),
        with_keys=frozenset({"body", "requires"}),
        input_type="pull-request",
        needs_body=True,
    ),
    "post-comment": V2OperationSpec(
        business=("posted",),
        with_keys=frozenset({"body", "requires"}),
        input_type=None,
        needs_body=True,
    ),
}
"""The closed first-release operation catalog (§10.1)."""

REQUIRABLE: frozenset[tuple[str, str]] = frozenset({("run-validation", "passed")})
"""The only requirable evidence in the first release (§10.1)."""

END_NODE_PREFIX = "~end:"
"""Reserved end-node prefix (§8.5). It contains characters outside
`[a-z0-9-]`, so no step id matching `[a-z0-9]+(-[a-z0-9]+)*` can collide with
it; the id check below enforces the guarantee structurally."""

DEFAULT_POLL_INTERVAL_SECONDS = 60.0
"""Default idle sleep between selection sweeps (§2.1)."""

DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0
"""Default SIGTERM grace for the live Attempt (§2.1)."""

DEFAULT_MAX_PARALLEL_SUBAGENTS = 2
"""Default concurrent Sub-agents per group (§2.2)."""


# --- Typed shape ---------------------------------------------------------------


@dataclass(frozen=True)
class V2Prices:
    """A Pinned Model's per-bucket prices in USD per million tokens (§2.1)."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class V2ModelDecl:
    """One `models.<id>` entry: a Pinned Model (§2.1).

    A direct pin names ``provider``; a gateway pin names ``gateway_kind``
    (with the canonical ``gateway_url``, or — resolved at ``run`` startup —
    the ``gateway_url_env`` name it is read from) and carries its own
    ``output_cap_tokens`` and ``context_threshold_tokens``. Exactly one of
    the two shapes is ever set, and a gateway pin carries exactly one of
    ``gateway_url`` or ``gateway_url_env``.
    """

    id: str
    provider: str | None
    model: str
    endpoint: str
    effort: str
    api_key_env: str
    prices: V2Prices
    gateway_kind: str | None = None
    gateway_url: str | None = None
    gateway_url_env: str | None = None
    output_cap_tokens: int | None = None
    context_threshold_tokens: int | None = None


@dataclass(frozen=True)
class V2Target:
    """The `target` block: which repository, branch and credential (§2.1)."""

    kind: str
    repository: str
    base_branch: str
    token_env: str


@dataclass(frozen=True)
class V2Worker:
    """The file-level `worker` settings with the §2.1 defaults applied."""

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS


@dataclass(frozen=True)
class V2Input:
    """A Workflow's input selector (§2.2, §5)."""

    type: str
    label: str
    require_all: tuple[str, ...] = ()
    require_any: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    drafts: str | None = None
    skip_open_agent_pr: bool | None = None


@dataclass(frozen=True)
class V2Ceiling:
    """A Workflow's Attempt Ceiling: all four meters, all required (§2.2)."""

    cost_usd: float
    tokens: float
    tool_calls: float
    wall_clock_minutes: float

    def by_meter(self) -> dict[str, float]:
        """The ceiling as meter name to allowance."""
        return {
            "cost-usd": self.cost_usd,
            "tokens": self.tokens,
            "tool-calls": self.tool_calls,
            "wall-clock-minutes": self.wall_clock_minutes,
        }


@dataclass(frozen=True)
class V2ListType:
    """A `{list: T}` payload field type (§2.4)."""

    item: str


@dataclass(frozen=True)
class V2EnumType:
    """An `{enum: [...]}` payload field type (§2.4)."""

    values: tuple[str, ...]


V2FieldType = Union[str, V2ListType, V2EnumType]
"""A payload field type: a scalar name, `{list: T}` or `{enum: [...]}`."""


@dataclass(frozen=True)
class V2Instruction:
    """One `instructions` entry: exactly one of `skill` or `prompt` (§9.3)."""

    skill: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class V2InputRef:
    """One declared step input: a reference, optionally optional (§2.5)."""

    reference: str
    optional: bool = False


@dataclass(frozen=True)
class V2Agent:
    """An `agent` executor block (§2.3, §9)."""

    instructions: tuple[V2Instruction, ...] = ()
    tools: tuple[str, ...] = ()
    inputs: tuple[V2InputRef, ...] = ()
    results: dict[str, dict[str, V2FieldType]] = field(default_factory=dict)


@dataclass(frozen=True)
class V2RouteTarget:
    """One Route target: a step id or a `{end: <category>}` Workflow End (§8)."""

    step: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class V2Step:
    """One Workflow Step: exactly one executor plus the control keys (§2.3)."""

    id: str
    agent: V2Agent | None = None
    operation: str | None = None
    with_body: str | None = None
    with_draft: bool | None = None
    with_requires: tuple[tuple[str, str], ...] = ()
    max_executions: int = 1
    limits: dict[str, float] = field(default_factory=dict)
    reserved: bool = False
    routes: dict[str, V2RouteTarget] = field(default_factory=dict)


@dataclass(frozen=True)
class V2Workflow:
    """One `workflows.<id>` entry (§2.2)."""

    id: str
    model: str
    input: V2Input
    ceiling: V2Ceiling
    max_parallel_subagents: int = DEFAULT_MAX_PARALLEL_SUBAGENTS
    outcomes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    start: str = ""
    defaults_max_executions: int = 1
    defaults_routes: dict[str, V2RouteTarget] = field(default_factory=dict)
    steps: dict[str, V2Step] = field(default_factory=dict)



# --- `skills` declaration (`version: 3`) -------------------------------------

@dataclass(frozen=True)
class V3SkillDecl:
    """One ``skills`` entry: where its members come from (§2.1, SKV3-02).

    ``source`` is the normalised installer Source Identity (always the
    ``git+https://`` spelling): both spellings of one URL normalise to it,
    and there is no equivalence by redirect, ``.git`` suffix or case.
    ``include`` names the exact member skills (case, spaces and underscores
    preserved — never trimmed, folded or matched against a Workflow-id
    regex). ``ref`` pins the entry (a branch, tag or commit); ``None`` floats
    on the source's default.
    """

    source: str
    include: tuple[str, ...]
    ref: str | None = None


@dataclass(frozen=True)
class V3Config:
    """A validated `version: 3` Workflow Configuration file (§2.1–§2.3).

    Structural shape only at the type level; reference, trust, environment
    and graph checks live in `coding_agent.workflow._checks` and run
    before one of these is returned. A missing ``skills`` field and
    ``skills: []`` both mean an empty Skill Bundle: there is no implicit
    four-skill default (SKV3-04, static part).
    """

    version: int
    target: V2Target
    models: dict[str, V2ModelDecl]
    worker: V2Worker
    workflows: dict[str, V2Workflow]
    skills: tuple[V3SkillDecl, ...]
    source: Path


def skill_membership(config: V3Config) -> dict[str, tuple[str, str | None]]:
    """The normalised membership: exact skill name -> (source, ref).

    This is what static ``instructions`` and on-demand availability consume:
    the exact declared name maps to its source entry's normalised Source
    Identity and optional ref. Names are exact strings — lookup never trims,
    folds or pattern-matches them.
    """
    membership: dict[str, tuple[str, str | None]] = {}
    for entry in config.skills:
        for name in entry.include:
            membership[name] = (entry.source, entry.ref)
    return membership


def normalize_skill_source(raw: str) -> str:
    """Normalise a ``skills`` entry's ``source`` to its Source Identity.

    Accepts HTTPS Git only, in the ``https://`` or ``git+https://`` spelling;
    the normalised form always carries the ``git+`` prefix (the installer's
    Source Identity). Anything else — SSH, local directories, other schemes —
    and any user-info, query or fragment component is refused with
    ``ValueError``.

    Static and offline: no subprocess runs and nothing is logged. Diagnostics
    never echo the raw value, so a credential-bearing component (user-info)
    cannot leak through an error message.
    """
    from urllib.parse import urlsplit

    if not isinstance(raw, str) or not raw:
        raise ValueError("skill source must be a non-empty string")
    if raw.startswith("git+https://"):
        https_part = raw[len("git+") :]
    elif raw.startswith("https://"):
        https_part = raw
    else:
        raise ValueError(
            "skill source must use 'https://' or 'git+https://' (HTTPS Git only)"
        )
    # Presence, not truthiness: an empty but present component
    # (`https://host/repo?`, `https://host/repo#`, `https://@host/repo`)
    # is still a query, fragment or user-info component (SKV3-03).
    if "#" in https_part:
        raise ValueError("skill source must not include a fragment component")
    if "?" in https_part:
        raise ValueError("skill source must not include a query component")
    try:
        parts = urlsplit(https_part)
    except ValueError as exc:
        raise ValueError("skill source does not parse") from exc
    if "@" in parts.netloc:
        raise ValueError("skill source must not include userinfo")
    host = parts.hostname or ""
    if not host:
        raise ValueError("skill source must include a host")
    if parts.path in ("", "/"):
        raise ValueError("skill source must include a repository path")
    return f"git+{https_part}"
