"""The production Attempt run: one claimed Attempt through one Workflow run.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (the
Attempt run to its recorded Terminal Outcome) and §18 (every registered
secret passes through the one redaction registry; git authenticates
through a credential helper while the remote URL stays the canonical
credential-free form).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from coding_agent.attempt_id import AttemptId
from coding_agent.identity.commit_identity import noreply_author
from coding_agent.inputs.claim import ConfirmedClaim
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.services import SocketServiceProber
from coding_agent.provider.observed_identity import PIN_VIOLATION_REASON
from coding_agent.runtime.git import (
    checkout_workspace,
    ensure_mirror,
    head_revision,
)
from coding_agent.runtime.graph.agent_step import AgentStepDeps, StepAttemptFacts
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import (
    WorkflowResult,
    run_workflow,
)
from coding_agent.runtime.redaction import temporary_git_credential_helper
from coding_agent.runtime.step_finalisation import (
    AttemptDecision,
    FinalisationTarget,
    GateStopped,
    InternalFailure,
    PinViolated,
    WorkflowEnded,
    count_uncommitted_files,
    finish_attempt,
)
from coding_agent.runtime.step_limits import build_budgets
from coding_agent.runtime.step_operations import (
    LiveCommitPushGitOps,
    LocalOperationRunner,
    tree_identity,
)
from coding_agent.runtime.step_publication import PublishingOperationRunner
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.worker.startup import Ready

WorkflowRunner = Callable[..., WorkflowResult]
"""Runs the Workflow graph for one Attempt (production or a test fake)."""


def run_attempt(
    ready: Ready,
    claimed: ConfirmedClaim,
    *,
    workflow_runner: WorkflowRunner = run_workflow,
) -> WorkflowResult:
    """Run one claimed Attempt to its recorded Terminal Outcome (§4).

    Takes every input from ``ready`` and the claim: the mirror and the
    workspace stay real. The evidence directory layout, the transcript
    path, the provenance footer digest (taken from the Run Ledger's
    recorded Effective Configuration digest, never the current file) and
    the printed ``run:`` lines are unchanged from the inline run.
    """
    assert isinstance(claimed, ConfirmedClaim)
    pin_facts = claimed.pin
    canonical_remote = ready.canonical_remote
    registry = ready.registry
    with temporary_git_credential_helper(ready.token) as helper:
        ensure_mirror(
            ready.mirror_dir,
            canonical_remote,
            redact=registry,
            helper=helper,
        )
        workspace = checkout_workspace(
            ready.mirror_dir,
            ready.workspace_dir,
            origin_url=canonical_remote,
            branch=ready.config.target.base_branch,
        )
        evidence_dir = (
            ready.state_dir
            / "evidence"
            / ready.owner
            / ready.repo
            / str(claimed.input_number)
            / str(claimed.n)
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        transcript = TranscriptWriter(
            evidence_dir / TRANSCRIPT_FILE_NAME,
            registry,
        )
        facts = StepAttemptFacts(
            attempt_id=claimed.attempt_id,
            workflow_id=ready.workflow.id,
            input_type=pin_facts.input_type,
            input_number=claimed.input_number,
            title=pin_facts.title,
            body=pin_facts.body,
            base_revision=pin_facts.base_revision,
            current_revision=pin_facts.base_revision,
            base_branch=pin_facts.base_branch
            or ready.config.target.base_branch,
            head_branch=pin_facts.head_branch,
            input_head=pin_facts.input_head,
        )
        attempt_budgets = build_budgets(
            ready.workflow,
            prices=ready.price,
            output_cap=ready.output_cap,
            attempt_id=claimed.attempt_id,
            is_gateway=ready.pin.is_gateway,
        )
        author_name, author_email = noreply_author(ready.identity)
        branch = AttemptId(
            ready.workflow.id,
            claimed.input_number,
            claimed.n,
        ).branch(pin_facts.title)
        local_runner = LocalOperationRunner(
            workspace=workspace.path,
            start_commit=head_revision(workspace.path),
            matrix=ready.matrix,
            service_env={},
            prober=SocketServiceProber(),
            runner=ready.command_runner,
            evidence_dir=evidence_dir,
            author_name=author_name,
            author_email=author_email,
            attempt_branch=branch,
            input_type=str(pin_facts.input_type),
            head_branch=pin_facts.head_branch,
            expected_head=pin_facts.input_head,
            input_number=claimed.input_number,
            git=LiveCommitPushGitOps(
                canonical_remote, helper=helper, redact=registry
            ),
            remote_url=canonical_remote,
            helper=helper,
            redact=registry,
        )
        profile = local_runner._pinned_profile()
        if profile is None:
            raise ValueError(
                f"cannot run Attempt {claimed.attempt_id!r}: "
                "no pinned Validation Contract profile at the Attempt commit "
                "(production must supply the profile; "
                "the shell and tests tools bind against it)"
            )
        live = ready.live
        phase_hook = live.set_phase if live is not None else None
        deps = AgentStepDeps(
            attempt=facts,
            model=TranscriptModel(ready.model, transcript),
            workspace=workspace.path,
            tree_id=tree_identity(workspace.path),
            activation_registry=ready.activation_registry,
            prompt_texts=ready.prompt_texts,
            ledger=ready.ledger,
            budgets=attempt_budgets,
            prices=ready.price,
            output_cap=ready.output_cap,
            context_window_threshold=ready.context_threshold_tokens,
            pin=ready.pin,
            is_gateway=ready.pin.is_gateway,
            redactions=registry,
            attempt_branch=branch,
            profile=profile,
            runner=ready.command_runner,
            evidence_dir=evidence_dir,
            on_activity=(live.touch if live is not None else None),
        )
        # The Attempt id is known at Claim: resolve the archive directory once
        # here, never per step inside the graph.
        deps.archive_dir = ready.ledger.archive_dir_for(claimed.attempt_id)
        # The provenance footer carries the Attempt's own Effective
        # Configuration digest, stored in the Run Ledger at Claim
        # (§2.10, §10, CFG-41) — never the current file's bytes.
        operation_runner = PublishingOperationRunner(
            local=local_runner,
            tracker=ready.tracker,
            workflow_id=ready.workflow.id,
            config_digest=ready.ledger.get_attempt(
                claimed.attempt_id
            ).effective_configuration_digest,
            issue_title=pin_facts.title,
            base_branch=ready.config.target.base_branch,
            base_revision=pin_facts.base_revision,
        )
        step_executor = ProductionStepExecutor(
            agent_deps=deps,
            operation_runner=operation_runner,
        )
        result = workflow_runner(
            workflow=ready.workflow,
            attempt_id=claimed.attempt_id,
            ledger=ready.ledger,
            step_executor=step_executor,
            budgets=attempt_budgets,
            phase_hook=phase_hook,
        )
        _finalise_live_attempt(
            tracker=ready.tracker,
            ledger=ready.ledger,
            result=result,
            number=claimed.input_number,
            input_type=pin_facts.input_type,
            selection_label=ready.workflow.input.label,
            end_labels_for=ready.workflow.outcomes,
            workspace_path=workspace.path,
        )
        print(
            f"run: {result.outcome} ({result.reason}); "
            f"attempt={result.attempt_id}"
        )
        print(f"run: transcript {transcript.path}")
        return result


def _finalise_live_attempt(
    *,
    tracker: StepTrackerPort,
    ledger: RunLedger,
    result: WorkflowResult,
    number: int,
    input_type: InputType,
    selection_label: str,
    end_labels_for: Mapping[str, tuple[str, ...]],
    workspace_path: Path,
) -> None:
    """Publish the live Attempt's terminal writes after the Workflow run.

    The graph never records a Terminal Outcome: this builds the outcome
    decision from the run result — a reached Workflow End, a Gate stop, a
    pin violation, or an internal failure — and ends the Attempt through
    the finalisation entry point, which records once before publishing
    (§15.2, §15.3).
    """
    if result.ended:
        decision: AttemptDecision = WorkflowEnded(
            category=result.outcome,
            reason=result.reason,
            ending_step=result.ending_step,
            step_result=result.step_result,
            workflow_end=result.workflow_end or result.outcome,
        )
    elif result.outcome in ("drift", "withdrawn"):
        decision = GateStopped(
            outcome=result.outcome,
            reason=f"{result.outcome}: {result.reason or result.outcome}",
        )
    elif (
        result.outcome == "interrupted"
        and result.reason == PIN_VIOLATION_REASON
    ):
        decision = PinViolated(step=result.ending_step)
    else:
        decision = InternalFailure(reason=result.reason)
    try:
        uncommitted = count_uncommitted_files(workspace_path)
    except Exception:
        uncommitted = 0
    finish_attempt(
        tracker=tracker,
        ledger=ledger,
        attempt_id=result.attempt_id,
        target=FinalisationTarget(
            number=number,
            input_type=input_type,
            selection_label=selection_label,
        ),
        decision=decision,
        end_labels_for=end_labels_for,
        uncommitted_count=uncommitted,
    )


__all__ = [
    "WorkflowRunner",
    "run_attempt",
]
