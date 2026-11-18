"""``agent run``: the Worker Supervisor's production wiring.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order — validate the Workflow Configuration, take the exclusive volume lock,
open the operator store, load the Toolchain Matrix, verify the Skill Bundle,
check the selected secrets' presence, run the non-mutating
GitHub checks of §18, run the Provider Capability Assertion, open the Run
Ledger, then settle and finalise every unfinished Attempt; a refusal at any
step exits non-zero with no Attempt opened and no GitHub write; startup
recovery refuses on a legacy Attempt; the poll loop — sleep for the poll
interval only when a sweep claimed nothing, select again immediately after
an Attempt ends; SIGTERM stops intake at once and the process then exits;
intake — while the ledger holds an Attempt that is not ``finished`` or
``released`` the Worker selects no new work; halting — a Credential Failure
halts at once with a non-zero exit, three consecutive
infrastructure-class failures halt only after the third Attempt is
finalized, the counter resetting after any Attempt without such a failure).

Matrix row ids: CFG-38, CFG-41, CFG-42, REC-11, OPC-7; and the retained
lifecycle rows (see the acceptance matrix).

Startup itself lives in :mod:`coding_agent.worker.startup`: :func:`start`
runs the ordered phases behind one call with exactly one refusal path, and
:func:`run_worker` only wires the :class:`Ready` it returns into the poll
loop. The Supervisor is plain Python, not a graph, and it never writes to
GitHub itself: the Claim (``inputs.claim``) and the Workflow Steps own every
write.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from coding_agent.identity.commit_identity import noreply_author
from coding_agent.inputs.numbering import attempt_branch
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.observed_identity import PIN_VIOLATION_REASON
from coding_agent.profile.services import SocketServiceProber
from coding_agent.runtime.git import (
    GitFailure,
    checkout_workspace,
    ensure_mirror,
    head_revision,
)
from coding_agent.runtime.graph.agent_step import AgentStepDeps, StepAttemptFacts
from coding_agent.runtime.graph import workflow_graph as _workflow_graph
from coding_agent.runtime.graph.workflow_graph import WorkflowResult
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
from coding_agent.worker import run as _worker_run
from coding_agent.worker.run import (
    WorkerLoopConfig,
)
from coding_agent.worker.ports import WorkerPorts, production_ports
from coding_agent.worker.startup import (
    Ready as Ready,
    SkillDeploymentCheck as SkillDeploymentCheck,
    StartupRefusal as StartupRefusal,
    _emit_startup_event as _emit_startup_event,
    configured_pin as configured_pin,
    credential_env_vars as credential_env_vars,
    github_tracker as github_tracker,
    start as start,
)


def run_worker(
    *,
    config: Path,
    workflow: str | None,
    state_dir: Path,
    skill_deployment: SkillDeploymentCheck,
    toolchain_matrix_path: Path | None = None,
    ports: WorkerPorts | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
) -> int:
    """Start the Worker in contract order, then run its poll loop (§4).

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4. Startup
    runs in :func:`coding_agent.worker.startup.start`; a refusal there exits
    non-zero here with no Attempt opened and no GitHub write. Only
    ``version: 3`` runs: a ``version: 2`` file is refused at step 1 with a
    migration diagnostic before installation, live calls, Claim or GitHub
    writes, and a ``version: 1`` file with the drain instruction (§16.4).
    SIGTERM handling is installed first so the process observes the signal
    even as PID 1 in the image (§4, §12).

    ``ports`` selects the startup collaborators: production uses the live
    adapters and passes nothing. ``sleep``, ``max_sweeps`` and
    ``max_attempts`` stay as loop seams.
    """
    started = start(
        config=config,
        workflow=workflow,
        state_dir=state_dir,
        skill_deployment=skill_deployment,
        toolchain_matrix_path=toolchain_matrix_path,
        ports=ports if ports is not None else production_ports(),
    )
    if isinstance(started, StartupRefusal):
        return 1
    ready: Ready = started
    with ready:
        author_name, author_email = noreply_author(ready.identity)

        def run_one_attempt(claimed: object) -> WorkflowResult:
            """Run one claimed Attempt to its recorded Terminal Outcome (§4).

            Contract: ``docs/contract/step-workflow-runtime-contract.md``
            §18 (every registered secret passes through the one redaction
            registry; git authenticates through a credential helper while
            the remote URL stays the canonical credential-free form).
            """
            from coding_agent.inputs.claim import ConfirmedClaim

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
                    output_cap=ready.output_cap or 0,
                    attempt_id=claimed.attempt_id,
                    is_gateway=ready.pin.is_gateway,
                )
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
                )
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
                    attempt_branch=attempt_branch(
                        ready.workflow.id,
                        claimed.input_number,
                        claimed.n,
                        pin_facts.title,
                    ),
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
                result = _workflow_graph.run_workflow(
                    workflow=ready.workflow,
                    attempt_id=claimed.attempt_id,
                    ledger=ready.ledger,
                    agent_deps=deps,
                    budgets=attempt_budgets,
                    operation_runner=operation_runner,
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

        # The poll loop settles unfinished Attempts before the first
        # selection (§4.1, §16.1); ``run_one_attempt`` covers the Workflow
        # run itself.
        return _worker_run.run_worker_loop(
            tracker=ready.tracker,
            ledger=ready.ledger,
            loop_config=WorkerLoopConfig(
                claim_config=ready.claim_config,
                workflow=ready.workflow,
                poll_interval_seconds=ready.config.worker.poll_interval_seconds,
                live=ready.live,
                merge_base_resolver=_merge_base_resolver(ready.mirror_dir),
            ),
            run_one_attempt=run_one_attempt,
            sleep=sleep,
            max_sweeps=max_sweeps,
            max_attempts=max_attempts,
        )


def _merge_base_resolver(
    mirror_dir: Path,
) -> Callable[[str, str], str]:
    """Resolve the merge base of a pull-request head and the base tip (§7.2).

    Runs ``git merge-base`` in the mirror directory; a failure raises
    ``GitFailure``, which the poll loop counts as an infrastructure-class
    failure and retries on the next sweep (§4.4).
    """

    def resolve(head_sha: str, base_tip: str) -> str:
        completed = subprocess.run(
            ["git", "merge-base", head_sha, base_tip],
            cwd=mirror_dir,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise GitFailure(
                f"'git merge-base' failed in {mirror_dir}: "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout.strip()

    return resolve


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
