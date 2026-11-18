"""A scripted :class:`StepExecutor` for graph control-flow tests.

Answers every Step Execution from a per-step queue of Step Result names, in
order. Records no Effect Intents or Effect Receipts: control flow only.
"""

from __future__ import annotations

from coding_agent.ledger.store import RunLedger
from coding_agent.runtime.graph.step_executor import StepOutcome
from coding_agent.workflow.schema import WorkflowStep


class ScriptExhausted(AssertionError):
    """A step's queue of scripted Step Results ran dry (a test-harness error)."""


class ScriptedStepExecutor:
    """Answer Step Executions from per-step queues of Step Results."""

    def __init__(self, script: dict[str, list[str]] | None = None) -> None:
        self.script: dict[str, list[str]] = {
            step_id: list(results) for step_id, results in (script or {}).items()
        }

    def execute(
        self,
        step: WorkflowStep,
        seq: int,
        attempt_id: str,
        ledger: RunLedger,
    ) -> StepOutcome:
        """Pop the next scripted Step Result for the step."""
        _ = (seq, attempt_id, ledger)
        queue = self.script.get(step.id)
        if not queue:
            raise ScriptExhausted(f"no scripted result left for step {step.id!r}")
        result = queue.pop(0)
        return StepOutcome(
            result=result,
            payload={"result": result},
            facts={},
            usage={},
            limit_fact=None,
        )
