"""The one Step executor port and its production adapter.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §8 (the
Workflow graph) and §9 (the ``agent`` step).

The graph answers every Step Execution through one port
(:class:`StepExecutor`): given the step, its sequence number, the Attempt id
and the Run Ledger, return a :class:`StepOutcome`. The graph routes and
records only — it never branches over executor kinds.

:class:`ProductionStepExecutor` is the production adapter. It wraps an
:class:`OperationRunner` and :func:`run_agent_step` (with its
:class:`AgentStepDeps`) and picks between them by step kind, owning the
mapping from the agent loop's status to a Step Result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    PriorStepExecution,
    run_agent_step,
)
from coding_agent.runtime.step_operations import OperationRunner
from coding_agent.workflow.schema import WorkflowStep


@dataclass(frozen=True)
class StepOutcome:
    """One Step Execution's answer, for the graph node to commit (§13.2)."""

    result: str
    payload: dict[str, object] | None
    facts: dict[str, object]
    usage: dict[str, object]
    limit_fact: dict[str, object] | None


class StepExecutor(Protocol):
    """Answer one Step Execution: the graph's one executor port (§8.5)."""

    def execute(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> StepOutcome:
        """Run one Step Execution and return its outcome.

        ``GateFailed``, ``PinViolation`` and ``WorkflowDefect`` propagate;
        the graph never routes them.
        """
        ...


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


@dataclass
class ProductionStepExecutor:
    """The production :class:`StepExecutor`: real agent and operation runs.

    Picks between the two runtimes by step kind: an ``agent`` step runs the
    agent loop with the Attempt's deps and priors; an ``operation`` step runs
    the operation runner, which covers the whole six-operation catalog. Owns
    the mapping from the agent loop's ``reported`` / ``limit-exceeded`` /
    ``error`` status to a Step Result.
    """

    agent_deps: AgentStepDeps | None = None
    operation_runner: OperationRunner | None = None

    def execute(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> StepOutcome:
        """Answer one Step Execution for real (§9, §10)."""
        from coding_agent.runtime.graph.workflow_graph import WorkflowDefect

        if step.agent is not None:
            if self.agent_deps is None:
                raise WorkflowDefect(
                    f"step {step.id!r} is an agent step but no agent deps were provided"
                )
            outcome = run_agent_step(
                step, seq, self.agent_deps, priors_from_ledger(ledger, attempt_id)
            )
            limit_fact = (
                dict(outcome.limit_fact) if outcome.limit_fact is not None else None
            )
            if outcome.status == "reported":
                assert outcome.result_name is not None
                return StepOutcome(
                    result=outcome.result_name,
                    payload=(
                        dict(outcome.payload) if outcome.payload is not None else {}
                    ),
                    facts=dict(outcome.summary),
                    usage=dict(outcome.usage),
                    limit_fact=limit_fact,
                )
            if outcome.status == "limit-exceeded":
                return StepOutcome(
                    result="limit-exceeded",
                    payload=None,
                    facts=dict(outcome.summary),
                    usage=dict(outcome.usage),
                    limit_fact=limit_fact,
                )
            return StepOutcome(
                result="error",
                payload=None,
                facts=dict(outcome.summary),
                usage=dict(outcome.usage),
                limit_fact=limit_fact,
            )
        if self.operation_runner is None:
            raise WorkflowDefect(
                f"step {step.id!r} is an operation step but no operation runner was provided"
            )
        execution = self.operation_runner.run(step, seq, attempt_id, ledger)
        return StepOutcome(
            result=execution.result,
            payload=dict(execution.payload),
            facts=dict(execution.facts),
            usage={},
            limit_fact=None,
        )


__all__ = [
    "ProductionStepExecutor",
    "StepExecutor",
    "StepOutcome",
    "priors_from_ledger",
]
