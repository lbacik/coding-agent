"""The step subgraph's exit policy: contract behaviour or the control arm.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 ("Exit": silence is
answered, not obeyed — a response with no tool calls and no accepted exit
draws one deterministic continuation message, budget two per work node) and
§9 (``report_completion`` is the only non-exceptional exit), and
[ADR 0014](../docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md).

[ADR 0019](../docs/adr/0019-the-silence-ends-stage-control-is-reachable-only-from-the-experiment-harness.md)
adds an experiment-only control arm beside the contract behaviour: the first
response with no tool calls ends the implementation stage, no silence
continuation is issued, and ``report_completion`` is absent from the model's
tool surface. Everything else — explicit activation, every other tool and
its schema, ceilings and Gates, Criterion Evidence via ``test_targeted``,
review, the Run Ledger and the Transcript — stays identical between arms.

This module is the seam's vocabulary only: it names the two policies and
computes the tool-schema digest the per-run record pins. The contract
behaviour is the default everywhere; only the experiment harness
(``coding_agent.experiment``) constructs the control policy. No Workflow
Configuration field, CLI flag or environment variable selects it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool

StepExitPolicyName = Literal["contract", "silence_ends_stage"]
"""The two exit policies the step subgraph understands.

``contract`` is today's §8 behaviour, unchanged: ``report_completion`` is
the only non-exceptional exit and silence draws up to two deterministic
continuations. ``silence_ends_stage`` is the experiment-only control arm
(ADR 0019): the first tool-less response ends the stage with no
continuation and ``report_completion`` off the tool surface.
"""


@dataclass(frozen=True)
class StepExitPolicy:
    """The step subgraph's exit rule, as a value with the contract default.

    The step subgraph takes this value at its seam
    (``build_implement_step_graph`` / ``run_work_node`` /
    ``run_implement_step``). The default is the contract behaviour; the
    control policy ends the stage on the first response with no tool calls
    and issues no silence continuation. Only the experiment harness
    constructs the control value.
    """

    name: StepExitPolicyName = "contract"

    def __post_init__(self) -> None:
        if self.name not in ("contract", "silence_ends_stage"):
            raise ValueError(f"unknown step exit policy {self.name!r}")


DEFAULT_STEP_EXIT_POLICY = StepExitPolicy("contract")
"""The contract behaviour: §8 exit, unchanged (ADR 0019)."""

SILENCE_ENDS_STAGE_EXIT_POLICY = StepExitPolicy("silence_ends_stage")
"""The experiment-only control arm (ADR 0019).

Reachable only from the experiment harness: no Workflow Configuration
field, CLI flag or environment variable selects it, and the ``agent
implement`` entry path never constructs it.
"""


def is_control_policy(policy: StepExitPolicy) -> bool:
    """Whether ``policy`` is the experiment-only control arm."""
    return policy.name == "silence_ends_stage"


def control_max_silence_continuations() -> int:
    """The control arm's silence budget: zero continuations (ADR 0019)."""
    return 0


def tools_for_policy(
    tools: Sequence[BaseTool], policy: StepExitPolicy
) -> list[BaseTool]:
    """The tool surface for ``policy``: identical, except the control arm.

    The control arm leaves ``report_completion`` off the model's tool
    surface (ADR 0019); every other tool object is passed through untouched.
    The contract arm returns the tools unchanged.
    """
    if not is_control_policy(policy):
        return list(tools)
    return [tool for tool in tools if tool.name != "report_completion"]


def tool_schema_digest(tools: Sequence[BaseTool]) -> str:
    """Digest the tool schema actually offered, for the per-run record.

    The digest covers each tool's name, description and argument schema as
    JSON bytes in name order, so the record pins exactly what the model saw.
    A tool whose schema cannot be serialised contributes its ``repr``.
    """
    entries: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda candidate: candidate.name):
        try:
            args_schema = dict(tool.args)
        except Exception:
            args_schema = {"repr": repr(tool.args)}
        try:
            entries.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "args": json.loads(
                        json.dumps(args_schema, sort_keys=True, default=str)
                    ),
                }
            )
        except (TypeError, ValueError):
            entries.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "args": {"repr": repr(args_schema)},
                }
            )
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
