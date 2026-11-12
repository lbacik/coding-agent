"""Compile a validated Workflow into a LangGraph `StateGraph`.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (the
Workflow graph: entry, Routes, Execution Ceilings, Workflow Ends and the
LangGraph mapping) and §13.2 (Step Executions and transitions).

Slice S3 of ``docs/plan/step-workflow-implementation-plan.md``. This module
replaces ``build_attempt_graph`` for the new runtime; the MVP graph is
untouched until the S10 cutover. It reuses the ``AttemptContext`` pattern:
runtime handles (the ledger, the scripted executors) travel in
``RunnableConfig["configurable"]`` via :class:`WorkflowRunContext`, never in
graph state.

Matrix row ids: CFG-40, ROUTE-1 … ROUTE-11, STEP-10, REC-2.

The mapping (§8.5):

- **Nodes:** one node per Workflow Step, its callable taken from the trusted
  executor registry by executor kind (``agent`` / ``operation``). The node
  first checks the Execution Ceiling; a refused entry creates no Step
  Execution and yields the refusal, which routes through the step's
  ``limit-exceeded`` Route. Where a run injects an ``operation_runner``
  (slices S7a–S7b), the local operations ``check-readiness``,
  ``run-validation`` and ``commit-and-push`` and the publishing operations
  ``open-pull-request``, ``publish-review`` and ``post-comment`` execute for
  real; every other operation — and every run without a runner — answers
  from the run's scripted results.
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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import (
    PIN_VIOLATION_DETAIL,
    PIN_VIOLATION_REASON,
    PinViolation,
)
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    PriorStepExecution,
    run_agent_step,
)
from coding_agent.runtime.step_finalisation import (
    render_terminal_comment,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.step_limits import AttemptBudgets
from coding_agent.runtime.step_operations import (
    LOCAL_OPERATIONS,
    GateFailed,
    OperationRunner,
)
from coding_agent.runtime.step_publication import PUBLISHING_OPERATIONS
from coding_agent.workflow.v2_schema import (
    END_NODE_PREFIX,
    OPERATIONS,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)

WRITE_OPERATIONS = frozenset(
    {"commit-and-push", "open-pull-request", "publish-review", "post-comment"}
)
"""Operations whose stub records an Effect Intent and Receipt (§14.1).

Read-only operations (``check-readiness``, ``run-validation``) record no
effect. The stub writes one ordinal (``0``) per Step Execution.
"""


class WorkflowState(TypedDict, total=False):
    """The compiled Workflow graph's data plane only.

    Step ids, the last Step Result, per-step execution counts and the
    Terminal Outcome under construction travel here. Handles (the ledger,
    the scripted results) must never appear in this state — they reach
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


class WorkflowScriptExhausted(AssertionError):
    """The run's scripted results ran out for a step (a test-harness error)."""


@dataclass
class WorkflowRunContext:
    """Everything a Workflow graph node needs that must not live in state.

    The S3 analogue of ``AttemptContext``: the real Run Ledger handle and
    the scripted executor results travel here, inside
    ``RunnableConfig["configurable"]``, while the graph state carries only
    the Attempt's small data-plane fields. ``script`` maps each step id to
    its queued Step Results; every execution pops the head of its step's
    queue, so re-entries are scripted in order.
    """

    ledger: RunLedger
    workflow: V2Workflow
    script: dict[str, list[str]] = field(default_factory=dict)
    agent_deps: AgentStepDeps | None = None
    budgets: AttemptBudgets | None = None
    operation_runner: OperationRunner | None = None
    phase_hook: Callable[[str], None] | None = None

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

    def next_result(self, step_id: str) -> str:
        """Pop the next scripted Step Result for ``step_id``."""
        queue = self.script.get(step_id)
        if not queue:
            raise WorkflowScriptExhausted(
                f"no scripted result left for step {step_id!r}"
            )
        return queue.pop(0)


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


def step_result_names(workflow: V2Workflow, step: V2Step) -> list[str]:
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


def effective_routes(workflow: V2Workflow, step: V2Step) -> dict[str, V2RouteTarget]:
    """The step's Routes after applying ``defaults.routes`` (§8.1).

    A step's own Route wins over the default. ``defaults.routes`` names
    only the runtime-reserved results, so business results always come
    from the step itself (proven by static validation, §3).
    """
    names = step_result_names(workflow, step)
    merged: dict[str, V2RouteTarget] = {
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


def target_node_name(target: V2RouteTarget) -> str:
    """The graph node one Route target maps to: a step or an end node."""
    if target.step is not None:
        return target.step
    assert target.end is not None
    return end_node_name(target.end)


def workflow_recursion_limit(workflow: V2Workflow) -> int:
    """The ``recursion_limit`` backstop for one compiled Workflow (§8.4).

    Real Step Executions are bounded by the sum of Execution Ceilings
    (§8.3); on top every step may take one refusal pass along the acyclic
    ``limit-exceeded`` chain plus the Workflow End node, so the bound is
    the sum of ceilings plus one pass over the steps. It is only a
    backstop: reaching it is a runtime defect ending ``internal-failure``.
    """
    ceilings = sum(step.max_executions for step in workflow.steps.values())
    return ceilings + len(workflow.steps) + 1


def used_end_categories(workflow: V2Workflow) -> list[str]:
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


def describe_workflow_graph(workflow: V2Workflow) -> WorkflowGraphSpec:
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


# --- Executor registry --------------------------------------------------------

StepExecutor = Callable[[V2Step, WorkflowRunContext, int], str]
"""One executor-kind callable answering a Step Execution (S3: from script)."""


def _agent_executor(step: V2Step, context: WorkflowRunContext, seq: int) -> str:
    """Answer one ``agent`` Step Execution (§9): the real loop where S4 deps
    are injected, else the run's script (the S3 stub kept for control-flow
    tests that inject no deps).

    The control flow around it — ceiling, recording, routing — is final in
    both cases.
    """
    _ = seq
    return context.next_result(step.id)


def priors_from_ledger(
    ledger: RunLedger, attempt_id: str
) -> dict[str, PriorStepExecution]:
    """The latest Step Execution of every step, for input resolution (§9.7)."""
    latest: dict[str, PriorStepExecution] = {}
    for record in ledger.list_step_executions(attempt_id):
        payload = dict(record.payload) if record.payload is not None else None
        facts = dict(record.facts) if record.facts is not None else None
        latest[record.step_id] = PriorStepExecution(
            step_id=record.step_id,
            seq=record.seq,
            result=record.result or "",
            payload=payload,
            summary=facts,
        )
    return latest


def _operation_executor(step: V2Step, context: WorkflowRunContext, seq: int) -> str:
    """Answer one ``operation`` Step Execution from the run's script.

    Write operations additionally record their Effect Intent and Receipt
    (§14.1) with write ordinal ``0``; read-only operations record no
    effect. Slice S7 replaces these stubs with the real catalog operation
    by operation: S7a runs the local operations for real where a run
    injects an ``operation_runner``, and every other operation still
    answers from the script here.
    """
    assert step.operation is not None
    result = context.next_result(step.id)
    if step.operation in WRITE_OPERATIONS:
        attempt_id = _current_attempt_id.get()
        if attempt_id is not None:
            context.ledger.record_step_effect_intent(
                attempt_id,
                step.id,
                seq,
                0,
                rendered_payload=f"{step.operation} by {step.id}#{seq}: {result}",
                expected_target={"operation": step.operation},
            )
            context.ledger.record_step_effect_receipt(
                attempt_id, step.id, seq, 0, remote_id=f"stub-{step.id}-{seq}"
            )
    return result


EXECUTOR_REGISTRY: dict[str, StepExecutor] = {
    "agent": _agent_executor,
    "operation": _operation_executor,
}
"""The trusted executor registry: executor kind to node callable (§8.5).

Executors are taken from here by kind, never from configuration or step
content. Both entries are S3 stubs answering from the run script; later
slices replace them without changing the graph shape.
"""


_current_attempt_id: ContextVar[str | None] = ContextVar(
    "workflow_attempt_id", default=None
)
"""The Attempt id of the Step Execution in flight, for effect stubs.

LangGraph nodes run in the caller's synchronous context, so a private
context variable carries the id from the node into the operation stub
without crossing the graph-state boundary.
"""


def _executor_kind(step: V2Step) -> str:
    return "agent" if step.agent is not None else "operation"


def _emit_native(context: WorkflowRunContext, attempt_id: str, event_type: str, text: str) -> None:
    """Emit one Attempt-scoped Contract Event, never failing the Attempt (S9, §4.5).

    :meth:`RunLedger.emit_attempt_event` is already best-effort; this keeps
    the graph node readable at the four native-event sites.
    """
    try:
        context.ledger.emit_attempt_event(attempt_id, event_type, text)
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
        from coding_agent.operator.attempts import write_execution_summary

        archive_dir = context.ledger.archive_dir_for(attempt_id)
        if archive_dir is None:
            return
        write_execution_summary(archive_dir, step_id, seq, dict(summary))
    except Exception:
        pass


def _make_step_node(workflow: V2Workflow, step: V2Step) -> Any:
    """Build the graph node for one Workflow Step (§8.5).

    The node first checks the Execution Ceiling (§8.3); a refused entry
    creates no Step Execution, records the ``execution-ceiling`` transition
    fact and yields the refusal, which routes through the step's
    ``limit-exceeded`` Route. Otherwise it opens the Step Execution (§13.2),
    answers it through the executor registry (operations stubbed), commits
    the end before the router runs, and records the Route taken where the
    target is another step. A Workflow End target is left for the end node
    to record. A result outside the step's path map raises
    :class:`WorkflowDefect` (§8.4): it is never routed through ``error``.

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
        from coding_agent.operator.attempts import (
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
        )
        _report_phase(context, f"{step.id}#{seq}")
        if context.agent_deps is not None and context.agent_deps.archive_dir is None:
            try:
                resolved = context.ledger.archive_dir_for(attempt_id)
            except Exception:
                resolved = None
            if resolved is not None:
                try:
                    context.agent_deps.archive_dir = resolved
                except Exception:
                    pass
        executor = EXECUTOR_REGISTRY[_executor_kind(step)]
        reset = _current_attempt_id.set(attempt_id)
        payload: dict[str, object] | None = None
        facts: dict[str, object] | None = None
        usage: dict[str, object] | None = None
        limit_fact: dict[str, object] | None = None
        try:
            if (
                step.operation is not None
                and step.operation in LOCAL_OPERATIONS | PUBLISHING_OPERATIONS
                and context.operation_runner is not None
            ):
                execution = context.operation_runner.run(
                    step, seq, attempt_id, context.ledger
                )
                result = execution.result
                payload = dict(execution.payload)
                facts = dict(execution.facts)
            elif context.agent_deps is not None and step.agent is not None:
                outcome = run_agent_step(
                    step, seq, context.agent_deps, priors_from_ledger(context.ledger, attempt_id)
                )
                limit_fact = (
                    dict(outcome.limit_fact) if outcome.limit_fact is not None else None
                )
                if outcome.status == "reported":
                    assert outcome.result_name is not None
                    result = outcome.result_name
                    payload = (
                        dict(outcome.payload) if outcome.payload is not None else {}
                    )
                elif outcome.status == "limit-exceeded":
                    result = "limit-exceeded"
                    payload = None
                else:
                    result = "error"
                    payload = None
                facts = dict(outcome.summary)
                usage = dict(outcome.usage)
            else:
                result = str(executor(step, context, seq))
                payload = {"result": result}
        finally:
            _current_attempt_id.reset(reset)
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
            payload=payload,
            facts=facts if facts is not None else {"seq": seq},
            usage=usage if usage is not None else {},
            limit_fact=limit_fact,
        )
        _persist_execution_summary(
            context,
            attempt_id,
            step.id,
            seq,
            dict(facts) if facts is not None else {"seq": seq},
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
    S" — and the Terminal Outcome, which is X (§8.2, §15.1), then leads to
    ``END``.
    """

    def end_node(state: WorkflowState, config: RunnableConfig) -> dict[str, object]:
        from coding_agent.operator.attempts import NATIVE_WORKFLOW_END

        context = WorkflowRunContext.from_config(config)
        attempt_id = str(state["attempt_id"])
        from_step = str(state.get("last_step") or "")
        result = str(state.get("last_result") or "")
        context.ledger.record_workflow_end(
            attempt_id, from_step=from_step, result=result, end=category
        )
        reason = f"ended at end {category} after result {result} of step {from_step}"
        try:
            from coding_agent.operator.step_projection import describe_limit_fact

            for record in context.ledger.list_step_executions(attempt_id):
                if record.limit_fact:
                    reason = describe_limit_fact(record.limit_fact)
                    break
        except Exception:
            pass
        context.ledger.record_terminal_outcome(
            attempt_id, outcome=category, reason=reason
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


def build_workflow_graph(workflow: V2Workflow) -> CompiledStateGraph[Any, Any, Any, Any]:
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


def _internal_failure(ledger: RunLedger, attempt_id: str, reason: str) -> WorkflowResult:
    """End the Attempt ``internal-failure`` (§8.4): never routed through ``error``."""
    ledger.record_terminal_outcome(attempt_id, outcome="internal-failure", reason=reason)
    return WorkflowResult(attempt_id=attempt_id, outcome="internal-failure", reason=reason)


def run_workflow(
    *,
    workflow: V2Workflow,
    attempt_id: str,
    ledger: RunLedger,
    script: dict[str, list[str]] | None = None,
    recursion_limit: int | None = None,
    agent_deps: AgentStepDeps | None = None,
    budgets: AttemptBudgets | None = None,
    operation_runner: OperationRunner | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> WorkflowResult:
    """Run one Attempt of a validated Workflow in-process (the S3 seam).

    Opens the Attempt row where none exists, invokes the compiled graph
    with the run's scripted results, and returns the recorded Terminal
    Outcome. A result outside the step's path map and a
    ``GraphRecursionError`` both end the Attempt ``internal-failure``
    (§8.4): neither is routed through ``error``. A :class:`GateFailed`
    from a local or publishing operation's Gate before a write ends the
    Attempt with the runtime-only outcome it carries (``drift`` or
    ``withdrawn``, §7.3): it is never routed either. Slices S7a–S7b add
    ``operation_runner``; runs without one keep answering every operation
    from the script.
    """
    if not ledger.has_attempt(attempt_id):
        ledger.open_attempt(
            attempt_id, effective_configuration={"workflow": workflow.id}
        )
    graph = build_workflow_graph(workflow)
    context = WorkflowRunContext(
        ledger=ledger, workflow=workflow, script={k: list(v) for k, v in (script or {}).items()},
        agent_deps=agent_deps,
        budgets=budgets,
        operation_runner=operation_runner,
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
        ledger.record_terminal_outcome(
            attempt_id, outcome=exc.status, reason=f"{exc.status}: {exc.reason}"
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome=exc.status,
            reason=exc.reason,
            step_result=exc.status,
            workflow_end=exc.status,
        )
    except PinViolation as exc:
        # A pin violation is never routed: the in-flight request is
        # discarded, open Step Executions close as interrupted, and the
        # Attempt ends ``interrupted`` / ``pin-violation`` through the
        # §12 finalisation (§9.1). The first record wins; the Supervisor
        # supersedes the comment with its final bytes before publishing.
        ledger.close_interrupted_step_executions(
            attempt_id, reason=PIN_VIOLATION_REASON
        )
        comment = render_terminal_comment(
            attempt_id=attempt_id,
            outcome="interrupted",
            ending_step=exc.step_id,
            detail=PIN_VIOLATION_DETAIL,
        )
        ledger.record_terminal_outcome(
            attempt_id,
            outcome="interrupted",
            reason=PIN_VIOLATION_REASON,
            terminal_comment_body=comment,
            writes=list(terminal_writes_for_outcome()),
        )
        return WorkflowResult(
            attempt_id=attempt_id,
            outcome="interrupted",
            reason=PIN_VIOLATION_REASON,
            ending_step=exc.step_id,
        )
    except WorkflowDefect as exc:
        ledger.close_interrupted_step_executions(
            attempt_id, reason="internal-failure"
        )
        return _internal_failure(ledger, attempt_id, f"internal-failure: {exc}")
    except GraphRecursionError:
        ledger.close_interrupted_step_executions(
            attempt_id, reason="internal-failure"
        )
        return _internal_failure(
            ledger,
            attempt_id,
            "internal-failure: GraphRecursionError "
            "(the Workflow graph exceeded its recursion backstop)",
        )
    assert isinstance(final, dict)
    outcome = str(final.get("outcome") or "internal-failure")
    reason_text = str(final.get("reason") or "no Workflow End was reached")
    if ledger.get_outcome(attempt_id) is None:
        ledger.record_terminal_outcome(
            attempt_id, outcome=outcome, reason=reason_text
        )
    recorded = ledger.get_outcome(attempt_id)
    assert recorded is not None
    return WorkflowResult(
        attempt_id=attempt_id,
        outcome=recorded.outcome,
        reason=recorded.reason,
        ending_step=str(final.get("last_step") or ""),
        step_result=str(final.get("last_result") or ""),
        workflow_end=recorded.outcome,
    )
