"""Typed Workflow Configuration objects (`version: 4`).

`version: 3` is no longer supported — `agent config validate`
and `agent run` refuse it with a migration diagnostic.


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

from coding_agent.workflow._fields import GUIDE, ID_PATTERN, described

# --- Vocabulary shared with the validator (and later slices) -----------------

RESERVED_RESULTS: tuple[str, ...] = ("error", "limit-exceeded")
"""Step Results the runtime produces; a Workflow never declares them (§2.4)."""

HARD_CONTEXT_RESULT = "hard-context-threshold"
"""The Step Result selecting the Hard Handoff Route (§2.4).

Reserved to the runtime only on agent steps with a ``context-thresholds``
policy: such a step never declares it, and the runtime reports it when the
next main-conversation request would cross the hard Usable Threshold. Steps
without the policy keep it as an ordinary business result spelling
(``soft-context-threshold`` is likewise reserved on any opted-in step, so
the spelling can never shadow the runtime result).
"""

SOFT_CONTEXT_RESULT = "soft-context-threshold"
"""The Step Result selecting the AI-assisted Handoff Route (§2.4).

Reserved to the runtime on any agent step with a ``context-thresholds``
policy, including a hard-only one: such a step never declares it, and the
runtime reports it when the next main-conversation request would cross the
soft Usable Threshold without crossing the hard one. Steps without the
policy keep it as an ordinary business result spelling.
"""

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

METERS: tuple[str, ...] = ("cost-usd", "effective-tokens", "tool-calls", "wall-clock-minutes")
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

ROUND_HANDOFF_KINDS: tuple[str, ...] = ("ai-assisted", "hard")
"""Allowed `round-handoff` annotation kinds on a publishing Route (§8).

``ai-assisted`` publishes the declared note Step Artifact; ``hard`` publishes
the model-free runtime account. Only the spelling is shared here; placement
and proof live in ``coding_agent.workflow._checks``.
"""

ROUND_HANDOFF_NOTE_REF = "round-handoff.note"
"""The special Continuation note input, declared optional on possible first
agent steps of an issue Workflow (§2.5). Absent on a fresh Attempt; rejected
in pull-request Workflows."""

INPUT_COMMENTS_REF = "input.comments"
"""The reserved declared-input reference for an issue's Trusted Comments (§7.4).

Accepted on any agent step of an issue Workflow, required or optional: the
input is always present (an empty list is present, not absent). Rejected in
pull-request Workflows and as an operation body reference."""

DEFAULT_ROUND_HANDOFF_PICKUP_LABEL = "round-finished"
"""The default Pickup Label for an opted-in issue Workflow (§8).

An enabled Workflow without an explicit ``pickup-label`` publishes and picks
up under this label. Explicit and implicit forms canonicalise identically in
the Effective Configuration digest."""


@dataclass(frozen=True)
class OperationSpec:
    """One catalog Runtime Operation: its business results, allowed `with`
    keys, input-type restriction and whether it publishes a body (§10.1)."""

    business: tuple[str, ...]
    with_keys: frozenset[str]
    input_type: str | None
    needs_body: bool


OPERATIONS: dict[str, OperationSpec] = {
    "check-readiness": OperationSpec(
        business=("ready", "not-ready"),
        with_keys=frozenset(),
        input_type=None,
        needs_body=False,
    ),
    "run-validation": OperationSpec(
        business=("passed", "failed"),
        with_keys=frozenset(),
        input_type=None,
        needs_body=False,
    ),
    "commit-and-push": OperationSpec(
        business=("pushed", "no-changes"),
        with_keys=frozenset({"requires"}),
        input_type=None,
        needs_body=False,
    ),
    "update-from-base": OperationSpec(
        business=("merged", "conflicted", "up-to-date"),
        with_keys=frozenset(),
        input_type="pull-request",
        needs_body=False,
    ),
    "open-pull-request": OperationSpec(
        business=("opened", "already-open"),
        with_keys=frozenset({"body", "draft", "requires"}),
        input_type="issue",
        needs_body=True,
    ),
    "publish-review": OperationSpec(
        business=("published", "stale-head"),
        with_keys=frozenset({"body", "requires"}),
        input_type="pull-request",
        needs_body=True,
    ),
    "post-comment": OperationSpec(
        business=("posted",),
        with_keys=frozenset({"body", "requires"}),
        input_type=None,
        needs_body=True,
    ),
}
"""The operation catalog (§10.1).

Closed in the first release; extended by one operation for ``pull-request``
Workflows only by ADR 0034 (``update-from-base``)."""

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
class ModelPrices:
    """A Pinned Model's per-bucket prices in USD per million tokens (§2.1)."""

    input: float = described(
        "input",
        "positive_number",
        required=True,
        description="The input price.",
    )
    output: float = described(
        "output",
        "positive_number",
        required=True,
        description="The output price.",
    )
    cache_read: float = described(
        "cache-read",
        "positive_number",
        required=True,
        description="The cache-read price.",
    )
    cache_write: float = described(
        "cache-write",
        "positive_number",
        required=True,
        description="The cache-write price.",
    )


@dataclass(frozen=True)
class ModelDecl:
    """One `models.<id>` entry: a Pinned Model (§2.1).

    A direct pin names ``provider``; a gateway pin names ``gateway_kind``
    (with the canonical ``gateway_url``, or — resolved at ``run`` startup —
    the ``gateway_url_env`` name it is read from). Every pin — direct or
    gateway — carries its own ``context_window_tokens`` (the Model Context
    Window a request and its response share), ``output_cap_tokens`` and
    ``context_threshold_tokens``. Exactly one of ``provider`` or
    ``gateway_kind`` is ever set, and a gateway pin carries exactly one of
    ``gateway_url`` or ``gateway_url_env``.
    """

    id: str
    provider: str | None = described(
        "provider",
        "custom",
        required=False,
        description="The model provider (direct pins only).",
        schema={"type": "string"},
    )
    model: str = described(
        "model",
        "custom",
        required=True,
        description=(
            "The pinned model id (the gateway's own name on a gateway pin)."
        ),
        schema={"type": "string"},
    )
    endpoint: str = described(
        "endpoint",
        "custom",
        required=True,
        description="The pinned endpoint (part of the pin).",
        schema={"type": "string"},
    )
    effort: str = described(
        "effort",
        "custom",
        required=True,
        description="The pinned effort level.",
        schema={"enum": sorted(EFFORTS)},
    )
    api_key_env: str = described(
        "api-key-env",
        "nonempty_str",
        required=True,
        description="The environment-variable name holding the key.",
    )
    prices: ModelPrices = described(
        "prices-usd-per-mtok",
        "custom",
        required=True,
        description="Per-bucket prices in USD per million tokens.",
    )
    gateway_kind: str | None = described(
        "gateway.kind",
        "custom",
        required=True,
        description="The gateway kind (today only `litellm`).",
        label="'gateway.kind'",
        schema={"type": "string"},
        default=None,
    )
    gateway_url: str | None = described(
        "gateway.url",
        "custom",
        required=False,
        description="The gateway URL (literal `http`/`https`).",
        label="'gateway.url'",
        schema={"type": "string", "minLength": 1},
        default=None,
    )
    gateway_url_env: str | None = described(
        "gateway.url-env",
        "nonempty_str",
        required=False,
        description=(
            "The environment-variable name holding the "
            "gateway URL (resolved at `run` startup)."
        ),
        label="'gateway.url-env'",
        default=None,
    )
    output_cap_tokens: int | None = described(
        "output-cap-tokens",
        "positive_int",
        required=True,
        description="The per-request output cap (every pin).",
        default=None,
    )
    context_threshold_tokens: int | None = described(
        "context-threshold-tokens",
        "positive_int",
        required=True,
        description="The context-window threshold (every pin).",
        default=None,
    )
    context_window_tokens: int | None = described(
        "context-window-tokens",
        "positive_int",
        required=True,
        description="The Model Context Window (every pin).",
        default=None,
    )


@dataclass(frozen=True)
class TargetRepository:
    """The `target` block: which repository, branch and credential (§2.1)."""

    kind: str = described(
        "kind",
        "custom",
        required=True,
        description="The target kind.",
        label="'target.kind'",
        schema={"enum": sorted(TARGET_KINDS)},
    )
    repository: str = described(
        "repository",
        "nonempty_str",
        required=True,
        description="The `OWNER/REPO` repository.",
        label="'target.repository'",
    )
    base_branch: str = described(
        "base-branch",
        "nonempty_str",
        required=True,
        description="The Base Revision branch.",
        label="'target.base-branch'",
    )
    token_env: str = described(
        "token-env",
        "nonempty_str",
        required=True,
        description="The environment-variable name holding the GitHub token.",
        label="'target.token-env'",
    )
    profile: str | None = described(
        "profile",
        "nonempty_str",
        required=False,
        description=(
            "The instance-supplied Project Profile: a path relative to the "
            "configuration file's directory, resolved like a `prompt:` "
            "locator. Where present it is the Validation Contract's source "
            "and the Target Repository's own profile is never read; where "
            "absent the profile comes from the Target Repository."
        ),
        label="'target.profile'",
        default=None,
    )


@dataclass(frozen=True)
class WorkerSettings:
    """The file-level `worker` settings with the §2.1 defaults applied."""

    poll_interval_seconds: float = described(
        "poll-interval-seconds",
        "positive_number",
        required=False,
        description="Idle sleep between selection sweeps.",
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    shutdown_grace_seconds: float = described(
        "shutdown-grace-seconds",
        "positive_number",
        required=False,
        description="SIGTERM grace for the live Attempt.",
        default=DEFAULT_SHUTDOWN_GRACE_SECONDS,
    )


@dataclass(frozen=True)
class InputDecl:
    """A Workflow's input selector (§2.2, §5)."""

    type: str = described(
        "type",
        "custom",
        required=True,
        description="Exactly one input type per Workflow.",
        schema={"enum": sorted(INPUT_TYPES)},
    )
    label: str = described(
        "label",
        "nonempty_str",
        required=True,
        description="The Selection Label.",
        label="'input.label'",
    )
    require_all: tuple[str, ...] = described(
        "require-all",
        "str_list",
        required=False,
        description="Narrowing filter: all of these labels.",
        default=(),
        schema_default=False,
    )
    require_any: tuple[str, ...] = described(
        "require-any",
        "str_list",
        required=False,
        description="Narrowing filter: any of these labels.",
        default=(),
        schema_default=False,
    )
    exclude: tuple[str, ...] = described(
        "exclude",
        "str_list",
        required=False,
        description="Excluded labels.",
        default=(),
        schema_default=False,
    )
    drafts: str | None = described(
        "drafts",
        "custom",
        required=False,
        description="Draft eligibility (pull-request inputs only).",
        schema={"enum": sorted(INPUT_DRAFTS)},
        default=None,
    )
    skip_open_agent_pr: bool | None = described(
        "skip-open-agent-pr",
        "bool",
        required=False,
        description=(
            "Skip an issue with an open agent pull request "
            "(issue inputs only)."
        ),
        default=None,
    )


@dataclass(frozen=True)
class AttemptCeiling:
    """A Workflow's Attempt Ceiling: all four meters, all required (§2.2)."""

    cost_usd: float = described(
        "cost-usd",
        "positive_number",
        required=True,
        description="The `cost-usd` ceiling.",
    )
    effective_tokens: float = described(
        "effective-tokens",
        "positive_number",
        required=True,
        description="The `effective-tokens` ceiling.",
    )
    tool_calls: float = described(
        "tool-calls",
        "positive_number",
        required=True,
        description="The `tool-calls` ceiling.",
    )
    wall_clock_minutes: float = described(
        "wall-clock-minutes",
        "positive_number",
        required=True,
        description="The `wall-clock-minutes` ceiling.",
    )

    def by_meter(self) -> dict[str, float]:
        """The ceiling as meter name to allowance."""
        return {
            "cost-usd": self.cost_usd,
            "effective-tokens": self.effective_tokens,
            "tool-calls": self.tool_calls,
            "wall-clock-minutes": self.wall_clock_minutes,
        }


@dataclass(frozen=True)
class ListType:
    """A `{list: T}` payload field type (§2.4)."""

    item: str


@dataclass(frozen=True)
class EnumType:
    """An `{enum: [...]}` payload field type (§2.4)."""

    values: tuple[str, ...]


FieldType = Union[str, ListType, EnumType]
"""A payload field type: a scalar name, `{list: T}` or `{enum: [...]}`."""


@dataclass(frozen=True)
class Instruction:
    """One `instructions` entry: exactly one of `skill` or `prompt` (§9.3)."""

    skill: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class InputRef:
    """One declared step input: a reference, optionally optional (§2.5)."""

    reference: str
    optional: bool = False


@dataclass(frozen=True)
class Agent:
    """An `agent` executor block (§2.3, §9)."""

    instructions: tuple[Instruction, ...] = ()
    tools: tuple[str, ...] = ()
    inputs: tuple[InputRef, ...] = ()
    results: dict[str, dict[str, FieldType]] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundHandoffAnnotation:
    """A step-local `round-handoff` annotation on a publishing Route (§8).

    Carried only on an `unfinished` Workflow End Route for `pushed` or
    `no-changes` from a `commit-and-push` step of an issue Workflow. With
    Round Handoff enablement omitted the annotation validates but the Route
    keeps ordinary `unfinished` semantics. The annotation never appears in
    pull-request Workflows or in `defaults.routes`.
    """

    kind: str
    """How the round publishes: `ai-assisted` or `hard`."""

    note: str | None = None
    """The published note reference (`<step>.<result>.<field>`).

    Required for `ai-assisted` (a declared `document` field proven available
    on every path to the publishing step); absent for `hard`.
    """


@dataclass(frozen=True)
class ContextThresholds:
    """An agent step's own Hard Context Threshold policy (§2.3).

    ``hard_tokens`` bounds the step's main conversation: crossing its Hard
    Usable Threshold ends the Step Execution with the
    ``hard-context-threshold`` Step Result. It is validated against the
    code-owned safety margin and request overhead and the Pinned Model's
    output cap and Model Context Window (contract §3).

    ``soft_tokens``, where present, is the step's Soft Context Threshold:
    strictly between the safety margin plus request overhead and
    ``hard_tokens``. Crossing its Soft Usable Threshold without crossing the
    hard one reports ``soft-context-threshold`` onto the step-local
    inheriting Route to the Reserved receiver step.
    """

    hard_tokens: int
    soft_tokens: int | None = None


@dataclass(frozen=True)
class RouteTarget:
    """One Route target: a step id or a `{end: <category>}` Workflow End (§8)."""

    step: str | None = None
    end: str | None = None
    inherit_conversation: bool = False
    """Whether this step Route inherits the source conversation (§8).

    Only ``True`` opts in; scalar step ids, omitted flags and explicit
    ``False`` all mean a fresh entry. Set only on step-local Routes
    between agent steps; end targets never carry it and ``defaults.routes``
    never names it.
    """
    round_handoff: RoundHandoffAnnotation | None = None
    """The step-local Round Handoff annotation, if the Route publishes one.

    Set only on an `unfinished` end target of a `pushed`/`no-changes` Route
    from `commit-and-push`. Step targets and `defaults.routes` never carry
    it; while enablement is omitted it keeps ordinary `unfinished` behavior.
    """


@dataclass(frozen=True)
class WorkflowStep:
    """One Workflow Step: exactly one executor plus the control keys (§2.3)."""

    id: str
    agent: Agent | None = described(
        "agent",
        "custom",
        required=False,
        description="An `agent` executor block.",
        default=None,
    )
    operation: str | None = described(
        "operation",
        "custom",
        required=False,
        description=(
            "A catalog Runtime Operation "
            f"({', '.join(sorted(OPERATIONS))}). See {GUIDE}."
        ),
        schema={"enum": sorted(OPERATIONS)},
        default=None,
    )
    with_body: str | None = None
    with_draft: bool | None = None
    with_requires: tuple[tuple[str, str], ...] = ()
    max_executions: int = described(
        "max-executions",
        "positive_int",
        required=False,
        description=(
            "The Execution Ceiling (default from `defaults`, else 1). "
            f"See {GUIDE}."
        ),
        default=1,
    )
    limits: dict[str, float] = described(
        "limits",
        "custom",
        required=False,
        description="The Step Allowance (agent steps only).",
        factory=dict,
    )
    reserved: bool = described(
        "reserved",
        "custom",
        required=False,
        description=(
            "A Reserved Step (agent steps only): declares all four meters. "
            f"See {GUIDE}."
        ),
        schema={"type": "boolean"},
        default=False,
    )
    context_thresholds: ContextThresholds | None = described(
        "context-thresholds",
        "custom",
        required=False,
        description=(
            "The step's own Hard Context Threshold policy (agent steps only): "
            "required `hard-tokens`. "
            f"See {GUIDE}."
        ),
        default=None,
    )
    routes: dict[str, RouteTarget] = described(
        "routes",
        "custom",
        required=True,
        description="Routes by result name.",
        factory=dict,
    )


@dataclass(frozen=True)
class RoundHandoffConfig:
    """An issue Workflow's Round Handoff enablement (§8).

    Present only when the Workflow opts into publication and automatic pickup.
    ``pickup_label`` is the resolved Pickup Label (explicit or the default
    ``round-finished``); ``max_automatic_pickups`` is the positive limit fixed
    by the human-authorised Attempt for the series.
    """

    max_automatic_pickups: int = described(
        "round-handoff.max-automatic-pickups",
        "positive_int",
        required=True,
        description=(
            "The positive limit on automatic round pickups per "
            f"human-authorised series. See {GUIDE}."
        ),
        label="'round-handoff.max-automatic-pickups'",
    )
    pickup_label: str = described(
        "round-handoff.pickup-label",
        "nonempty_str",
        required=False,
        description=(
            "The Pickup Label for automatic pickup "
            f"(default `{DEFAULT_ROUND_HANDOFF_PICKUP_LABEL}`). See {GUIDE}."
        ),
        label="'round-handoff.pickup-label'",
        default=DEFAULT_ROUND_HANDOFF_PICKUP_LABEL,
    )


@dataclass(frozen=True)
class Workflow:
    """One `workflows.<id>` entry (§2.2)."""

    id: str
    model: str = described(
        "model",
        "custom",
        required=True,
        description=(
            "The id of one `models` entry: the Attempt's one Pinned Model. "
            f"See {GUIDE}."
        ),
        schema={"type": "string", "pattern": ID_PATTERN},
    )
    input: InputDecl = described(
        "input",
        "custom",
        required=True,
        description="Which Workflow Inputs the Worker selects.",
    )
    ceiling: AttemptCeiling = described(
        "limits",
        "custom",
        required=True,
        description="The Attempt Ceiling: all four meters are required.",
    )
    max_parallel_subagents: int = described(
        "max-parallel-subagents",
        "int_ge_1",
        required=False,
        description=(
            "How many Sub-agents of one group may run at once. "
            f"See {GUIDE}."
        ),
        default=DEFAULT_MAX_PARALLEL_SUBAGENTS,
    )
    outcomes: dict[str, tuple[str, ...]] = described(
        "outcomes",
        "custom",
        required=False,
        description="End labels per Terminal Outcome.",
        factory=dict,
    )
    start: str = described(
        "start",
        "custom",
        required=True,
        description=f"The id of the first step. See {GUIDE}.",
        schema={"type": "string", "pattern": ID_PATTERN},
        default="",
    )
    defaults_max_executions: int = described(
        "defaults.max-executions",
        "positive_int",
        required=False,
        description="The default Execution Ceiling.",
        default=1,
    )
    defaults_routes: dict[str, RouteTarget] = field(default_factory=dict)
    round_handoff: RoundHandoffConfig | None = described(
        "round-handoff",
        "custom",
        required=False,
        description=(
            "Round Handoff enablement for an issue Workflow: publication and "
            "automatic pickup under a Pickup Label with a positive pickup "
            f"limit. Omitted disables both. See {GUIDE}."
        ),
        default=None,
    )
    steps: dict[str, WorkflowStep] = described(
        "steps",
        "custom",
        required=True,
        description="A map from step id to step.",
        factory=dict,
    )

    @property
    def referenced_pins(self) -> tuple[str, ...]:
        """The ids of every pin this Workflow references, in assertion order.

        Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4.1
        (the Provider Capability Assertion runs for every distinct pin the
        selected Workflow references). The single source the startup
        sequence and the Advisor slice use: the Workflow's own pin first,
        then any other referenced pin by id. Today no step can name another
        pin, so the set is exactly the Workflow's ``model``.
        """
        return (self.model,)



# --- `skills` declaration (`version: 4`) -------------------------------------

@dataclass(frozen=True)
class SkillBundleRequirement:
    """One ``skills`` entry: where its members come from (§2.1, SKV3-02).

    ``source`` is the normalised installer Source Identity (always the
    ``git+https://`` spelling): both spellings of one URL normalise to it,
    and there is no equivalence by redirect, ``.git`` suffix or case.
    ``include`` names the exact member skills (case, spaces and underscores
    preserved — never trimmed, folded or matched against a Workflow-id
    regex). ``ref`` pins the entry (a branch, tag or commit); ``None`` floats
    on the source's default.
    """

    source: str = described(
        "source",
        "nonempty_str",
        required=True,
        description=(
            "The skill source: an HTTPS Git URL in `https://` or "
            "`git+https://` spelling."
        ),
    )
    include: tuple[str, ...] = described(
        "include",
        "custom",
        required=True,
        description=(
            "The source's exact member skill names (case, spaces and "
            "underscores preserved)."
        ),
        schema={
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "string",
                "minLength": 1,
                "description": "One exact member skill name.",
            },
        },
    )
    ref: str | None = described(
        "ref",
        "nonempty_str",
        required=False,
        description=(
            "The entry's pin (a branch, tag or commit); absent floats on "
            "the source's default."
        ),
        default=None,
    )


@dataclass(frozen=True)
class WorkflowConfiguration:
    """A validated `version: 4` Workflow Configuration file (§2.1–§2.3).

    Structural shape only at the type level; reference, trust, environment
    and graph checks live in `coding_agent.workflow._checks` and run
    before one of these is returned. A missing ``skills`` field and
    ``skills: []`` both mean an empty Skill Bundle: there is no implicit
    four-skill default (SKV3-04, static part).
    """

    version: int
    target: TargetRepository
    models: dict[str, ModelDecl]
    worker: WorkerSettings
    workflows: dict[str, Workflow]
    skills: tuple[SkillBundleRequirement, ...]
    source: Path


def skill_membership(config: WorkflowConfiguration) -> dict[str, tuple[str, str | None]]:
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
