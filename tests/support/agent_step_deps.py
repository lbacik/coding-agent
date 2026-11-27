"""A complete ``AgentStepDeps`` factory for step-loop tests.

Tests drive the agent loop the way production does: a ``PinnedModel``
matching the ``ScriptedModel``'s reported identity, an
``ActivationRegistry`` built directly, a fake ``CommandRunner`` with a
minimal ``ProjectProfile``, and a real Run Ledger with ``AttemptBudgets``.
Every field is overridable by keyword; the defaults keep each test short
without per-test budgets or ledger plumbing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from coding_agent.ledger.store import RunLedger
from coding_agent.profile.schema import Check, Evidence, ProjectProfile, Toolchain
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.graph.agent_step import (
    AgentStepDeps,
    EvidenceStore,
    StepAttemptFacts,
)
from coding_agent.runtime.redaction import Redactable
from coding_agent.runtime.result_capping import ArtifactStore, InMemoryArtifactStore
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.runtime.step_limits import AttemptBudgets, build_budgets
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.validate.runner import CommandRunner, ExecutedCommand
from coding_agent.workflow.schema import (
    AttemptCeiling,
    InputDecl,
    Workflow,
)


def make_pin(model: str = "test-model") -> PinnedModel:
    """The Attempt's direct pin for scripted responses reporting ``model``."""
    return PinnedModel(
        provider="anthropic", model=model, endpoint="messages", effort="low"
    )


def make_prices() -> TokenPrices:
    """One dollar a megatoken in, two out; reads a tenth, writes at par."""
    return TokenPrices(input=1.0, output=2.0, cache_read=0.1, cache_write=1.0)


def make_profile(
    *,
    checks: tuple[Check, ...] = (),
    test_targeted: str = "run {path} --junit-xml={evidence_dir}/targeted.xml",
) -> ProjectProfile:
    """The minimal pinned Validation Contract binding ``shell``/``tests``."""
    return ProjectProfile(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="true",
        test_all="true",
        test_targeted=test_targeted,
        evidence=Evidence(
            format="junit-xml",
            test_all="{evidence_dir}/all.xml",
            test_targeted="{evidence_dir}/targeted.xml",
        ),
        checks=checks,
        services=(),
    )


class FakeCommandRunner(CommandRunner):
    """A ``CommandRunner`` recording commands without shelling out.

    Every run succeeds with ``stdout`` and the given exit code; tests
    needing Validation Contract evidence (a JUnit report on disk) pass
    their own runner instead.
    """

    def __init__(self, *, stdout: str = "ok", exit_code: int = 0) -> None:
        self.calls: list[str] = []
        self.stdout = stdout
        self.exit_code = exit_code

    @property
    def timeout_seconds(self) -> float:
        """This runner's per-command timeout (never hit: nothing runs)."""
        return 60.0

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
    ) -> ExecutedCommand:
        _ = (cwd, timeout_seconds)
        self.calls.append(command)
        return ExecutedCommand(
            command=command,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr="",
        )


def make_workflow(workflow_id: str = "implement") -> Workflow:
    """A workflow with a generous Attempt Ceiling for default budgets."""
    return Workflow(
        id=workflow_id,
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
    )


def make_budgets(
    workflow: Workflow,
    *,
    attempt_id: str,
    prices: TokenPrices | None = None,
    output_cap: int = 0,
) -> AttemptBudgets:
    """Live meter state for ``workflow`` that no scripted turn can cross."""
    return build_budgets(
        workflow,
        prices=prices if prices is not None else make_prices(),
        output_cap=output_cap,
        attempt_id=attempt_id,
    )


def make_attempt(**overrides: Any) -> StepAttemptFacts:
    """The Attempt facts one step's opening frame carries."""
    base: dict[str, Any] = {
        "attempt_id": "implement#7/1",
        "workflow_id": "implement",
        "input_type": "issue",
        "input_number": 7,
        "title": "Fix the crash",
        "body": "The worker crashes on startup.",
        "base_revision": "abc123",
        "current_revision": "abc123",
    }
    base.update(overrides)
    return StepAttemptFacts(
        attempt_id=str(base["attempt_id"]),
        workflow_id=str(base["workflow_id"]),
        input_type=str(base["input_type"]),
        input_number=int(base["input_number"]),
        title=str(base["title"]),
        body=str(base["body"]),
        base_revision=str(base["base_revision"]),
        current_revision=str(base["current_revision"]),
    )


def make_agent_step_deps(
    *,
    model: InvokableToolModel,
    workspace: Path,
    tmp_path: Path,
    attempt: StepAttemptFacts | None = None,
    pin: PinnedModel | None = None,
    activation_registry: ActivationRegistry | None = None,
    ledger: RunLedger | None = None,
    budgets: AttemptBudgets | None = None,
    prices: TokenPrices | None = None,
    output_cap: int = 0,
    profile: ProjectProfile | None = None,
    runner: CommandRunner | None = None,
    evidence_dir: Path | None = None,
    archive_dir: Path | None = None,
    prompt_texts: dict[str, str] | None = None,
    artifact_store: ArtifactStore | None = None,
    context_window: ContextWindowConfig | None = None,
    max_parallel_subagents: int = 2,
    redactions: Redactable = None,
    changed_files: tuple[str, ...] = (),
    attempt_branch: str | None = None,
    is_gateway: bool = False,
    evidence: EvidenceStore | None = None,
    on_activity: Callable[[], None] | None = None,
    is_stopped: Callable[[], bool] | None = None,
) -> AgentStepDeps:
    """A complete ``AgentStepDeps`` the way production builds it.

    Defaults mirror the Worker's Attempt run: the direct pin, the startup
    activation snapshot (empty here — pass ``activation_registry`` where the
    step names skills), the Run Ledger, Attempt Budgets under a generous
    ceiling, the price table, the output cap, and the Validation Contract
    profile with a fake command runner and a per-test evidence directory.
    Pass ``pin`` matching the ``ScriptedModel``'s reported identity where
    the responses report one.
    """
    resolved_attempt = attempt if attempt is not None else make_attempt()
    resolved_prices = prices if prices is not None else make_prices()
    resolved_ledger = ledger
    if resolved_ledger is None:
        resolved_ledger = RunLedger(tmp_path / "deps-ledger" / "run-ledger.sqlite3")
    resolved_profile = profile if profile is not None else make_profile()
    resolved_runner = runner if runner is not None else FakeCommandRunner()
    resolved_evidence_dir = evidence_dir
    if resolved_evidence_dir is None:
        resolved_evidence_dir = tmp_path / "evidence"
        resolved_evidence_dir.mkdir(parents=True, exist_ok=True)
    resolved_budgets = budgets
    if resolved_budgets is None:
        resolved_budgets = make_budgets(
            make_workflow(resolved_attempt.workflow_id),
            attempt_id=resolved_attempt.attempt_id,
            prices=resolved_prices,
            output_cap=output_cap,
        )
    resolved_archive_dir = archive_dir if archive_dir is not None else tmp_path / "archive"
    inner_model: InvokableToolModel = model
    if not isinstance(inner_model, TranscriptModel):
        inner_model = TranscriptModel(
            inner_model,
            TranscriptWriter(resolved_archive_dir / TRANSCRIPT_FILE_NAME, redactions),
        )
    deps = AgentStepDeps(
        attempt=resolved_attempt,
        model=inner_model,
        workspace=workspace,
        tree_id="tree-1",
        pin=pin if pin is not None else make_pin(),
        activation_registry=(
            activation_registry
            if activation_registry is not None
            else ActivationRegistry(bodies={})
        ),
        ledger=resolved_ledger,
        budgets=resolved_budgets,
        prices=resolved_prices,
        output_cap=output_cap,
        profile=resolved_profile,
        runner=resolved_runner,
        evidence_dir=resolved_evidence_dir,
        evidence=evidence if evidence is not None else EvidenceStore(),
        prompt_texts=dict(prompt_texts) if prompt_texts is not None else {},
        archive_dir=resolved_archive_dir,
        artifact_store=artifact_store if artifact_store is not None else InMemoryArtifactStore(),
        context_window=context_window,
        max_parallel_subagents=max_parallel_subagents,
        redactions=redactions,
        changed_files=changed_files,
        attempt_branch=attempt_branch,
        is_gateway=is_gateway,
        on_activity=on_activity,
    )
    if is_stopped is not None:
        deps.is_stopped = is_stopped
    from coding_agent.runtime.graph.agent_model_request import wire_meter_to_ledger

    wire_meter_to_ledger(
        resolved_budgets, resolved_ledger, attempt_id=resolved_attempt.attempt_id
    )
    return deps
