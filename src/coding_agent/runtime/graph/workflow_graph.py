"""Compile a validated Workflow into a LangGraph `StateGraph`.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (the
Workflow graph: entry, Routes, Execution Ceilings, Workflow Ends and the
LangGraph mapping) and §13.2 (Step Executions and transitions).

Slice S3 of ``docs/plan/step-workflow-implementation-plan.md``. This module
replaces ``build_attempt_graph`` for the new runtime; the MVP graph is
untouched until the S10 cutover. It reuses the ``AttemptContext`` pattern:
runtime handles (the ledger, the Step executor) travel in
``RunnableConfig["configurable"]`` via :class:`WorkflowRunContext`, never in
graph state.

Matrix row ids: CFG-40, ROUTE-1 … ROUTE-11, STEP-10, REC-2.

The mapping (§8.5):

- **Nodes:** one node per Workflow Step, answering through the run's one
  Step executor (:class:`StepExecutor`). The node first checks the Execution
  Ceiling; a refused entry creates no Step Execution and yields the refusal,
  which routes through the step's ``limit-exceeded`` Route.
- **Edges:** the only plain edge is ``add_edge(START, <start>)``. Every step
  has exactly one ``add_conditional_edges(step, router, path_map)`` whose
  keys are the step's result names after defaults and whose values are nodes.
  Steps are never joined by ``add_edge``. ``Send`` and ``Command`` are
  never used.
- **Ends:** each end category used gets a runtime-owned end node under the
  reserved end-node prefix, which records the ending and leads to ``END``.
- **Persistence:** ``checkpointer=None``. ``recursion_limit`` is derived
  from the sum of Execution Ceilings and is only a backstop (§8.4).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from coding_agent.runtime.graph.agent_step import InheritedConversation

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_REASON,
    PinViolation,
)
from coding_agent.runtime.exchange import ExchangeUnit
from coding_agent.runtime.graph.step_executor import StepExecutor
from coding_agent.runtime.graph.step_executor import StepOutcome as StepOutcome
from coding_agent.runtime.graph.step_executor import priors_from_ledger as priors_from_ledger
from coding_agent.runtime.step_finalisation import (
    limit_fact_text,
    workflow_end_reason,
)
from coding_agent.runtime.step_limits import AttemptBudgets
from coding_agent.runtime.step_operations import GateFailed
from coding_agent.workflow.schema import (
    END_NODE_PREFIX,
    OPERATIONS,
    RouteTarget,
    WorkflowStep,
    Workflow,
)



class WorkflowState(TypedDict, total=False):
    """The compiled Workflow graph's data plane only.

    Step ids, the last Step Result, per-step execution counts and the
    Terminal Outcome under construction travel here. Handles (the ledger,
    the Step executor) must never appear in this state — they reach
    nodes through :class:`WorkflowRunContext`. The structural test asserts
    the annotation names carry none of them.
    """

    attempt_id: str
    counts: dict[str, int]
    last_step: str
    last_result: str
    outcome: str
    reason: str


class WorkflowDefect(Exception):
    """A Step Execution returned a result outside its path map (§8.4).

    Never routed through ``error``: the runner ends the Attempt
    ``internal-failure``.
    """


@dataclass
class WorkflowRunContext:
    """Everything a Workflow graph node needs that must not live in state.

    The S3 analogue of ``AttemptContext``: the real Run Ledger handle and
    the Step executor travel here, inside ``RunnableConfig["configurable"]``,
    while the graph state carries only the Attempt's small data-plane fields.
    """

    ledger: RunLedger
    workflow: Workflow
    step_executor: StepExecutor
    budgets: AttemptBudgets | None = None
    phase_hook: Callable[[str], None] | None = None
    histories: dict[tuple[str, int], tuple[ExchangeUnit, ...]] = field(default_factory=dict)
    """In-memory source history by (step id, seq): complete Exchange Units (§8).

    Kept in memory only; the redacted Transcript remains the audit record.
    """
    opening_tails: dict[tuple[str, int], tuple[BaseMessage, ...]] = field(
        default_factory=dict
    )
    """In-memory opening tails by (step id, seq): after header and input (§8)."""

    @classmethod
    def from_config(cls, config: RunnableConfig | None) -> WorkflowRunContext:
        """Recover the context the caller passed to ``graph.invoke``."""
        configurable: dict[str, Any] = dict((config or {}).get("configurable") or {})
        context = configurable.get("workflow_context")
        assert isinstance(context, WorkflowRunContext), (
            "the Workflow graph requires a WorkflowRunContext under "
            "config['configurable']['workflow_context']"
        )
        return context

    def to_configurable(self) -> dict[str, object]:
        """Wrap this context for ``graph.invoke(state, config=...)``."""
        return {"workflow_context": self}


@dataclass(frozen=True)
class WorkflowResult:
    """The externally observable end of a Workflow run: Terminal Outcome."""

    attempt_id: str
    outcome: str
    reason: str
    ending_step: str = ""
    """The step whose result reached the Workflow End (§15.3)."""
    step_result: str = ""
    """The Step Result that reached the Workflow End (§15.3)."""
    workflow_end: str = ""
    """The Workflow End category reached (the outcome, §8.2)."""
    ended: bool = True
    """Whether a Workflow End was reached; without one the Supervisor ends
    the Attempt ``internal-failure``. The graph never records a Terminal
    Outcome: the Supervisor records it once through Attempt finalisation."""


def step_result_names(workflow: Workflow, step: WorkflowStep) -> list[str]:
    """Every result name a step can produce: business results plus reserved.

    Mirrors the validator's per-step result set (§3): an ``agent`` step's
    declared results, or the catalog results of an ``operation`` step (with
    the conditional ``stale-head`` and ``requirement-unmet`` where they
    apply), plus the runtime-reserved ``error`` and ``limit-exceeded``.
    """
    if step.agent is not None:
        names = list(step.agent.results)
    else:
        spec = OPERATIONS.get(step.operation or "")
        names = list(spec.business) if spec is not None else []
        if step.operation == "commit-and-push" and workflow.input.type == "pull-request":
            names.append("stale-head")
        if step.with_requires:
            names.append("requirement-unmet")
    names.extend(["error", "limit-exceeded"])
    return names


def effective_routes(workflow: Workflow, step: WorkflowStep) -> dict[str, RouteTarget]:
    """The step's Routes after applying ``defaults.routes`` (§8.1).

    A step's own Route wins over the default. ``defaults.routes`` names
    only the runtime-reserved results, so business results always come
    from the step itself (proven by static validation, §3).
    """
    names = step_result_names(workflow, step)
    merged: dict[str, RouteTarget] = {
        key: target
        for key, target in workflow.defaults_routes.items()
        if key in names
    }
    merged.update(step.routes)
    return merged


def end_node_name(category: str) -> str:
    """The runtime-owned end node for one Workflow End category (§8.5).

    The contract names the reserved end-node prefix ``~end:``; ``:`` is a
    reserved character in LangGraph node names, so the compiled graph
    spells the same prefix with ``.``. The leading ``~`` still sits outside
    ``[a-z0-9]+(-[a-z0-9]+)*``, so no step id can collide with it.
    """
    safe_prefix = END_NODE_PREFIX.replace(":", ".")
    return f"{safe_prefix}{category}"


def target_node_name(target: RouteTarget) -> str:
    """The graph node one Route target maps to: a step or an end node."""
    if target.step is not None:
        return target.step
    assert target.end is not None
    return end_node_name(target.end)


def workflow_recursion_limit(workflow: Workflow) -> int:
    """The ``recursion_limit`` backstop for one compiled Workflow (§8.4).

    Real Step Executions are bounded by the sum of Execution Ceilings
    (§8.3); on top every step may take one refusal pass along the acyclic
    ``limit-exceeded`` chain plus the Workflow End node, so the bound is
    the sum of ceilings plus one pass over the steps. It is only a
    backstop: reaching it is a runtime defect ending ``internal-failure``.
    """
    ceilings = sum(step.max_executions for step in workflow.steps.values())
    return ceilings + len(workflow.steps) + 1


def used_end_categories(workflow: Workflow) -> list[str]:
    """Every end category routed to, in first-use order (§8.5)."""
    seen: list[str] = []
    for step_id in workflow.steps:
        for target in effective_routes(workflow, workflow.steps[step_id]).values():
            if target.end is not None and target.end not in seen:
                seen.append(target.end)
    return seen


@dataclass(frozen=True)
class WorkflowGraphSpec:
    """The compiled shape of one Workflow, before LangGraph sees it.

    ``conditional_edges`` holds exactly one entry per step: the step id
    mapped to its path map (result name to node name). The builder below
    turns this spec into the ``StateGraph`` one edge at a time, so the
    structural test (CFG-40) can assert the shape without reading
    LangGraph internals.
    """

    start: str
    steps: tuple[str, ...]
    end_nodes: tuple[str, ...]
    plain_edges: tuple[tuple[str, str], ...]
    conditional_edges: dict[str, dict[str, str]]
    recursion_limit: int


def describe_workflow_graph(workflow: Workflow) -> WorkflowGraphSpec:
    """Describe the graph one validated Workflow compiles to (§8.5)."""
    conditional: dict[str, dict[str, str]] = {}
    for step_id, step in workflow.steps.items():
        conditional[step_id] = {
            result: target_node_name(target)
            for result, target in effective_routes(workflow, step).items()
        }
    return WorkflowGraphSpec(
        start=workflow.start,
        steps=tuple(workflow.steps),
        end_nodes=tuple(end_node_name(category) for category in used_end_categories(workflow)),
        plain_edges=((START, workflow.start),),
        conditional_edges=conditional,
        recursion_limit=workflow_recursion_limit(workflow),
    )


# --- Step executor port ----------------------------------------------------------
# :func:`priors_from_ledger` lives in ``step_executor`` (the production
# adapter builds it); it is re-exported here for its existing importers.


def _emit_native(
    context: WorkflowRunContext,
    attempt_id: str,
    event_type: str,
    text: str,
    *,
    phase: str | None = None,
) -> None:
    """Emit one Attempt-scoped Contract Event, never failing the Attempt (S9, §4.5).

    :meth:`RunLedger.emit_attempt_event` is already best-effort; this keeps
    the graph node readable at the four native-event sites. ``phase``
    carries the step phase for ``phase_changed`` events so the archive
    record matches the live view.
    """
    try:
        context.ledger.emit_attempt_event(attempt_id, event_type, text, phase=phase)
    except Exception:
        pass


def _report_phase(context: WorkflowRunContext, phase: str) -> None:
    """Tell the live view the native phase, never failing the Attempt (S9, §19)."""
    hook = context.phase_hook
    if hook is None:
        return
    try:
        hook(phase)
    except Exception:
        pass


def _persist_execution_summary(
    context: WorkflowRunContext,
    attempt_id: str,
    step_id: str,
    seq: int,
    summary: dict[str, Any],
) -> None:
    """Persist one Execution Summary into the Attempt Archive (S9, §9.7, §4.5).

    Best-effort like every archive write: the archive directory resolves
    through the ledger, and a failure to resolve or to serialise never
    fails the Attempt it describes.
    """
    try:
        from coding_agent.operator.archive import write_execution_summary

        archive_dir = context.ledger.archive_dir_for(attempt_id)
        if archive_dir is None:
            return
        write_execution_summary(archive_dir, step_id, seq, dict(summary))
    except Exception:
        pass


def _make_step_node(workflow: Workflow, step: WorkflowStep) -> Any:
    """Build the graph node for one Workflow Step (§8.5).

    The node first checks the Execution Ceiling (§8.3); a refused entry
    creates no Step Execution, records the ``execution-ceiling`` transition
    fact and yields the refusal, which routes through the step's
    ``limit-exceeded`` Route. Otherwise it opens the Step Execution (§13.2),
    answers it through the run's one Step executor, commits the end before
    the router runs, and records the Route taken where the target is another
    step. A Workflow End target is left for the end node to record. A result
    outside the step's path map raises :class:`WorkflowDefect` (§8.4): it is
    never routed through ``error``.

    Step-workflow operator projection (S9, §4.5, §19): the node emits the
    native step events — ``native.step_opened`` on open (with
    ``phase_changed`` to ``<step>#<seq>``), ``native.step_ended`` on end
    (persisting the Execution Summary first), ``native.refused_entry`` on
    a ceiling refusal and ``native.route_taken`` after a Route — so the
    Attempt Archive's ``events.jsonl`` carries the step lifecycle and the
    operator projection can read the native phase back.
    """
    routes = effective_routes(workflow, step)
    path_keys = set(routes)

    def step_node(state: WorkflowState, config: RunnableConfig) -> dict[str, object]:
        from coding_agent.operator.archive import (
            EVENT_PHASE_CHANGED,
            NATIVE_REFUSED_ENTRY,
            NATIVE_ROUTE_TAKEN,
            NATIVE_STEP_ENDED,
            NATIVE_STEP_OPENED,
        )

        context = WorkflowRunContext.from_config(config)
        attempt_id = str(state["attempt_id"])
        counts = dict(state.get("counts") or {})
        if context.budgets is not None and step.agent is not None:
            entry = context.budgets.check_entry(step.id)
            if entry is not None:
                context.ledger.record_refused_entry(
                    attempt_id, step_id=step.id, reason="limit-exceeded"
                )
                _emit_native(
                    context,
                    attempt_id,
                    NATIVE_REFUSED_ENTRY,
                    f"refused entry to {step.id}: limit-exceeded",
                )
                return {"last_step": step.id, "last_result": "limit-exceeded"}
        used = counts.get(step.id, 0)
        if used >= step.max_executions:
            context.ledger.record_refused_entry(
                attempt_id, step_id=step.id, reason="execution-ceiling"
            )
            _emit_native(
                context,
                attempt_id,
                NATIVE_REFUSED_ENTRY,
                f"refused entry to {step.id}: execution-ceiling",
            )
            return {"last_step": step.id, "last_result": "limit-exceeded"}
        seq = used + 1
        context.ledger.open_step_execution(
            attempt_id,
            step.id,
            seq,
            entry_from_step=state.get("last_step"),
            entry_result=state.get("last_result"),
        )
        _emit_native(
            context,
            attempt_id,
            NATIVE_STEP_OPENED,
            f"step opened {step.id}#{seq}",
        )
        _emit_native(
            context,
            attempt_id,
            EVENT_PHASE_CHANGED,
            f"phase {step.id}#{seq}",
            phase=f"{step.id}#{seq}",
        )
        _report_phase(context, f"{step.id}#{seq}")
        inherited: InheritedConversation | None = None
        prev_step_id = state.get("last_step")
        prev_result = state.get("last_result")
        if isinstance(prev_step_id, str) and isinstance(prev_result, str) and prev_step_id:
            prev_step = workflow.steps.get(prev_step_id)
            if prev_step is not None:
                incoming = effective_routes(workflow, prev_step).get(prev_result)
                if (
                    incoming is not None
                    and incoming.step == step.id
                    and incoming.inherit_conversation
                ):
                    prev_seq = int(counts.get(prev_step_id, 0))
                    units = context.histories.get((prev_step_id, prev_seq), ())
                    tail = context.opening_tails.get((prev_step_id, prev_seq), ())
                    from coding_agent.runtime.graph.agent_step import (
                        InheritedConversation as _Inherited,
                    )

                    inherited = _Inherited(
                        source_step=prev_step_id,
                        source_seq=prev_seq,
                        source_result=prev_result,
                        opening_tail=tuple(tail),
                        units=tuple(units),
                    )
        outcome = context.step_executor.execute(
            step, seq, attempt_id, context.ledger, inherited
        )
        context.histories[(step.id, seq)] = tuple(outcome.history)
        context.opening_tails[(step.id, seq)] = tuple(outcome.opening_tail)
        result = outcome.result
        if result not in path_keys:
            raise WorkflowDefect(
                f"step {step.id!r} returned {result!r}, outside its path map "
                f"({', '.join(sorted(path_keys))})"
            )
        context.ledger.end_step_execution(
            attempt_id,
            step.id,
            seq,
            result=result,
            payload=outcome.payload,
            facts=outcome.facts,
            usage=outcome.usage,
            limit_fact=outcome.limit_fact,
        )
        _persist_execution_summary(
            context,
            attempt_id,
            step.id,
            seq,
            dict(outcome.facts),
        )
        _emit_native(
            context,
            attempt_id,
            NATIVE_STEP_ENDED,
            f"step ended {step.id}#{seq}: {result}",
        )
        _emit_native(
            context,
            attempt_id,
            EVENT_PHASE_CHANGED,
            f"phase {step.id}#{seq}",
            phase=f"{step.id}#{seq}",
        )
        _report_phase(context, f"{step.id}#{seq}")
        counts[step.id] = seq
        update: dict[str, object] = {
            "counts": counts,
            "last_step": step.id,
            "last_result": result,
        }
        target = routes[result]
        if target.step is not None:
            context.ledger.record_route_taken(
                attempt_id, from_step=step.id, result=result, to_step=target.step
            )
            _emit_native(
                context,
                attempt_id,
                NATIVE_ROUTE_TAKEN,
                f"route taken {step.id} --{result}--> {target.step}",
            )
        return update

    step_node.__name__ = f"step_{step.id}"
    return step_node


def _make_end_node(category: str) -> Any:
    """Build the runtime-owned end node for one Workflow End category (§8.5).

    Records the Workflow End fact — "ended at end X after result R of step
    S" — and carries the Terminal Outcome, which is X (§8.2, §15.1), in
    memory only, then leads to ``END``. The graph never records a Terminal
    Outcome: the Supervisor records it once through Attempt finalisation
    (§15.2).
    """

    def end_node(state: WorkflowState, config: RunnableConfig) -> dict[str, object]:
        from coding_agent.operator.archive import NATIVE_WORKFLOW_END

        context = WorkflowRunContext.from_config(config)
        attempt_id = str(state["attempt_id"])
        from_step = str(state.get("last_step") or "")
        result = str(state.get("last_result") or "")
        context.ledger.record_workflow_end(
            attempt_id, from_step=from_step, result=result, end=category
        )
        reason = workflow_end_reason(
            from_step=from_step,
            result=result,
            end=category,
            limit_fact=limit_fact_text(context.ledger, attempt_id),
        )
        _emit_native(
            context,
            attempt_id,
            NATIVE_WORKFLOW_END,
            f"workflow end {category} after result {result} of step {from_step}",
        )
        return {"outcome": category, "reason": reason}

    end_node.__name__ = f"end_{category}"
    return end_node


def _make_router() -> Any:
    """Route by Step Result name only: no expressions, guards or counts (§8.1)."""

    def router(state: WorkflowState) -> str:
        return str(state.get("last_result") or "")

    return router


def build_workflow_graph(workflow: Workflow) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile a validated Workflow into its ``StateGraph`` (§8.5).

    One node per Workflow Step with exactly one conditional edge whose keys
    are the step's results after defaults; one end node per used category;
    the only plain edge is ``START -> start``; ``checkpointer=None``.
    Static validation (§3) guarantees coverage, so every router key the
    nodes can yield is present in its path map.
    """
    spec = describe_workflow_graph(workflow)
    builder: StateGraph[Any] = StateGraph(WorkflowState)
    for step_id, step in workflow.steps.items():
        builder.add_node(step_id, _make_step_node(workflow, step))
    for category in used_end_categories(workflow):
        builder.add_node(end_node_name(category), _make_end_node(category))
    builder.add_edge(START, spec.start)
    router = _make_router()
    for step_id, path_map in spec.conditional_edges.items():
        # `dict` is invariant: `dict[str, str]` is not a
        # `dict[Hashable, str]`, so the (equal) mapping is cast to the
        # facet LangGraph declares.
        builder.add_conditional_edges(
            step_id, router, cast("dict[Hashable, str]", path_map)
        )
    for end_node in spec.end_nodes:
        builder.add_edge(end_node, END)
    return builder.compile(checkpointer=None)


def run_workflow(
    *,
    workflow: Workflow,
    attempt_id: str,
    ledger: RunLedger,
    step_executor: StepExecutor,
    recursion_limit: int | None = None,
    budgets: AttemptBudgets | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> WorkflowResult:
    """Run one Attempt of a validated Workflow in-process.

    Opens the Attempt row where none exists, invokes the compiled graph
    with the run's Step executor, and returns the result with enough to
    build an outcome decision. The graph never records a Terminal Outcome:
    the end node records the Workflow End fact and the Supervisor records
    the outcome once through Attempt finalisation (§15.2). A result outside
    the step's path map and a ``GraphRecursionError`` both end the Attempt
    ``internal-failure`` (§8.4): neither is routed through ``error``. A
    :class:`GateFailed` from a local or publishing operation's Gate before
    a write ends the Attempt with the runtime-only outcome it carries
    (``drift`` or ``withdrawn``, §7.3): it is never routed either.
    """
    if not ledger.has_attempt(attempt_id):
        ledger.open_attempt(
            attempt_id, effective_configuration={"workflow": workflow.id}
        )
    if budgets is not None:
        try:
            from coding_agent.runtime.step_limits import restore_budgets_from_usage

            restore_budgets_from_usage(budgets, ledger.get_latest_usage(attempt_id))
        except Exception:
            pass
    graph = build_workflow_graph(workflow)
    context = WorkflowRunContext(
        ledger=ledger,
        workflow=workflow,
        step_executor=step_executor,
        budgets=budgets,
        phase_hook=phase_hook,
    )
    limit = (
        recursion_limit
        if recursion_limit is not None
        else workflow_recursion_limit(workflow)
    )
    try:
        final = graph.invoke(
            {"attempt_id": attempt_id, "counts": {}},
            config={
                "configurable": context.to_configurable(),
                "recursion_limit": limit,
            },
        )
    except GateFailed as exc:
        ledger.close_interrupted_step_executions(attempt_id, reason=exc.status)
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome=exc.status,
            reason=exc.reason,
            step_result=exc.status,
            workflow_end=exc.status,
            ended=False,
        )
    except PinViolation as exc:
        # A pin violation is never routed: the in-flight request is
        # discarded, open Step Executions close as interrupted, and the
        # Attempt ends ``interrupted`` / ``pin-violation`` through the
        # Supervisor's finalisation (§12, §9.1).
        ledger.close_interrupted_step_executions(
            attempt_id, reason=PIN_VIOLATION_REASON
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="interrupted",
            reason=PIN_VIOLATION_REASON,
            ending_step=exc.step_id,
            ended=False,
        )
    except WorkflowDefect as exc:
        ledger.close_interrupted_step_executions(
            attempt_id, reason="internal-failure"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="internal-failure",
            reason=f"internal-failure: {exc}",
            ended=False,
        )
    except GraphRecursionError:
        ledger.close_interrupted_step_executions(
            attempt_id, reason="internal-failure"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="internal-failure",
            reason="internal-failure: GraphRecursionError "
            "(the Workflow graph exceeded its recursion backstop)",
            ended=False,
        )
    assert isinstance(final, dict)
    outcome = str(final.get("outcome") or "")
    if not outcome:
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="internal-failure",
            reason="no Workflow End was reached",
            ending_step=str(final.get("last_step") or ""),
            step_result=str(final.get("last_result") or ""),
            ended=False,
        )
    return WorkflowResult(
        attempt_id=attempt_id,
        outcome=outcome,
        reason=str(final.get("reason") or ""),
        ending_step=str(final.get("last_step") or ""),
        step_result=str(final.get("last_result") or ""),
        workflow_end=outcome,
        ended=True,
    )
