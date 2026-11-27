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

import functools
import time
from collections.abc import Callable
from pathlib import Path

from coding_agent.inputs.pickup import PickupConfig, PickupRemoteVerifier
from coding_agent.runtime.round_handoff import RoundHandoffOffer
from coding_agent.runtime.target_mirror import TargetMirror
from coding_agent.worker import run as _worker_run
from coding_agent.worker.attempt_run import (
    WorkflowRunner as WorkflowRunner,
    run_attempt as run_attempt,
)
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
    workflow_runner: WorkflowRunner | None = None,
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
    ``max_attempts`` stay as loop seams. ``workflow_runner`` injects the
    Workflow run (production passes none of it and the Attempt run uses
    the real graph).
    """
    from coding_agent.operator.runlog import set_container_log_enabled

    # This process is the Worker: its stdout/stderr is the container log, so
    # committed Contract Events and model turns mirror there from here on
    # (the listener thread included). ``agentctl`` and the offline readers
    # never enable this — they own stdout for contract JSON.
    set_container_log_enabled(True)
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
        if workflow_runner is None:
            run_one_attempt = functools.partial(run_attempt, ready)
        else:
            run_one_attempt = functools.partial(
                run_attempt, ready, workflow_runner=workflow_runner
            )

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
                merge_base_resolver=_merge_base_resolver(ready),
                pickup_config=_verified_pickup_config(ready),
            ),
            run_one_attempt=run_one_attempt,
            sleep=sleep,
            max_sweeps=max_sweeps,
            max_attempts=max_attempts,
        )


def _verified_pickup_config(ready: Ready) -> PickupConfig | None:
    """The pickup parameters with the remote availability check attached (§8).

    Intake verifies the advertised revisions against the remote before any
    Claim: the named ref must still serve the recorded work commit with the
    recorded Base Revision in history (read in the Worker's mirror, which
    this check creates and updates on first use so a fresh state volume
    verifies against the live remote rather than a missing checkout). A
    moved ref or an unavailable revision refuses the offer as
    ``pickup-unavailable`` with the signal left untouched. ``None`` where the
    running Workflow leaves Round Handoff disabled. Each verification holds
    its own short-lived credential helper, so no token outlives the check.
    """
    from dataclasses import replace

    pickup = ready.pickup_config
    if pickup is None:
        return None

    mirror = TargetMirror(
        ready.mirror_dir, ready.canonical_remote, ready.token, ready.registry
    )

    def verify(offer: RoundHandoffOffer) -> bool:
        return mirror.offer_still_served(offer)

    verifier: PickupRemoteVerifier = verify
    return replace(pickup, verify_remote=verifier)


def _merge_base_resolver(
    ready: Ready,
) -> Callable[[str, str], str]:
    """Resolve the merge base of a pull-request head and the base tip (§7.2).

    Delegates to the Target Repository mirror, which brings itself up to
    date — cloning where the state volume is fresh, fetching where it
    already exists — under its own short-lived credential helper. A failure
    raises ``GitFailure`` after the Claim has opened, so the poll loop halts
    with ``claim-interrupted`` and leaves the unfinished Attempt for restart
    recovery (§4.4, §16.1); no later sweep retries it.
    """
    mirror = TargetMirror(
        ready.mirror_dir, ready.canonical_remote, ready.token, ready.registry
    )

    def resolve(head_sha: str, base_tip: str) -> str:
        return mirror.resolve_base_revision(head_sha, base_tip)

    return resolve
