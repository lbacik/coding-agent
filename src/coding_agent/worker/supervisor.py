"""``agent run``: the Worker Supervisor's startup and its production wiring.

Contract: ``docs/contract/mvp-runtime-contract.md`` §3 (startup in order —
validate the Workflow Configuration, take the exclusive volume lock, verify
the Skill Bundle, run the non-mutating GitHub checks of §18, run the
Provider Capability Assertion, then open the Run Ledger; a failure at any
step exits non-zero with no Attempt opened and no GitHub write; startup
recovery is M8), §2.5 (the Effective Configuration is resolved once at
startup and pinned on every Attempt the Worker claims), §5 (the Claim opens
the Attempt
the graph then runs), §7 (each Attempt's workspace is rebuilt from the
mirror at the configured base branch) and §18 (one identity for API calls
and for git; credentials never reach a Validation Contract command).

MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, L3-ERR-11, L3-ERR-12,
W-PIN-1, W-PIN-2, W-STP-7, P-CMD-2, L1-3, L1-4, L1-4a, P-CAN-3, REC-SIG-1, W-CFG-14,
REC-CFG-2, REC-OUT-1, REC-DEL-1, REC-DEL-2, REC-DEL-3, REC-DEL-4, P-REC-2,
REC-INT-1, REC-INT-2, REC-INT-3, REC-INT-4, REC-INT-5, REC-INT-6, P-REC-1,
P-REC-3, P-REC-4.

The Supervisor is plain Python, not a graph, and it never writes to GitHub
itself: the Claim (``worker.claim``) and the Attempt graph own every write.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import (
    Identity,
    StartupCheckFailed,
    run_startup_checks,
)
from coding_agent.identity.token import TokenRejected
from coding_agent.ledger.store import RunLedger
from coding_agent.operator.listener import OperatorListener
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import worker_socket_path
from coding_agent.operator.store import OPERATOR_BACKEND, OperatorStore, agent_version
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    load_supported_toolchain_matrix,
    resolve_toolchain_matrix_path,
)
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.config import DEFAULT_CONTEXT_WINDOWS
from coding_agent.provider.pinned_model import (
    Effort,
    Endpoint,
    PinnedModel,
    Provider,
    build_chat_model,
)
from coding_agent.provider.price_table import PriceTable, TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.git import (
    checkout_workspace,
    ensure_mirror,
    remote_ref_sha,
    target_repository_url,
)
from coding_agent.runtime.redaction import (
    SecretRegistry,
    install_stream_redaction,
    temporary_git_credential_helper,
)
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.graph.step import WorkflowStep
from coding_agent.runtime.pinned_prefix import context_window_for
from coding_agent.runtime.result_capping import DEFAULT_RESULT_CAP_LIMIT
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.skillbundle.verify import (
    VerificationReport,
    load_ignored_refs,
    verify_bundle,
)
from coding_agent.tracker.github import GitHubTracker
from coding_agent.tracker.port import TrackerPort
from coding_agent.validate.runner import CredentialStrippedCommandRunner
from coding_agent.workflow import (
    EffectiveConfiguration,
    WorkflowConfigError,
    effective_configuration_record,
    load_workflow_config,
    resolve_effective_configuration,
)
from coding_agent.worker.claim import ClaimConfig, ClaimedAttempt
from coding_agent.worker.recovery import (
    InterruptedRecovery,
    RecoveryRefused,
    validate_recovery_configs,
)
from coding_agent.worker.run import (
    HALT_CLAIM_INTERRUPTED,
    HALT_CREDENTIAL_FAILURE,
    HALT_INFRASTRUCTURE_FAILURE,
    HALT_RECOVERY_REFUSED,
    HALT_RECOVERY_WRITE_FAILURE,
    HALT_STARTUP_CHECK_REFUSED,
    WorkerLoopConfig,
    checkpoint_or_ignore,
    run_worker_loop,
)
from coding_agent.worker.volume import VolumeLockHeld, acquire_volume_lock

PROVIDER_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
"""The provider SDKs' default credential variables (§18)."""

DEFAULT_SKILL_BUNDLE_DIR = Path("/opt/coding-agent/skill-bundle")
"""Where the image keeps the build-time installer reports (Dockerfile)."""

DEFAULT_SKILL_BUNDLE_IGNORE_FILE = Path("/opt/coding-agent/skill-bundle-ignore.txt")
"""The reviewed-dependency register the image copies in (Dockerfile)."""


@dataclass(frozen=True)
class SkillBundleCheck:
    """What startup step 3 verifies the installed Skill Bundle against (§3).

    The two reports are the build-time ``agent-installer install --json``
    and ``list --json`` outputs; ``home`` is the runtime user's home the
    bundle was installed into.
    """

    install_report: Path
    list_report: Path
    home: Path
    ignore_file: Path


def verify_installed_skill_bundle(check: SkillBundleCheck) -> VerificationReport:
    """Verify the Skill Bundle the Worker will activate (§3 step 3, §18).

    Any unreadable report raises ``OSError`` or ``json.JSONDecodeError``;
    the caller turns that into a startup refusal.
    """
    install_report = json.loads(check.install_report.read_text(encoding="utf-8"))
    list_report = json.loads(check.list_report.read_text(encoding="utf-8"))
    ignored_refs = load_ignored_refs(check.ignore_file)
    return verify_bundle(install_report, list_report, check.home, ignored_refs)


def credential_env_vars(token_env: str, api_key_env: str | None = None) -> list[str]:
    """The variables stripped from every Validation Contract command (§18).

    The Agent Identity's token, the provider SDKs' default key variables and
    the Pinned Model's configured ``api_key_env``: a command from the Target
    Repository never sees a credential the Worker holds (L3-IMP-9).
    """
    names = [token_env, *PROVIDER_KEY_ENV_VARS]
    if api_key_env and api_key_env not in names:
        names.append(api_key_env)
    return names


def configured_pin(effective: EffectiveConfiguration) -> tuple[PinnedModel, TokenPrices]:
    """The Pinned Model and its per-bucket prices, from the Effective
    Configuration's referenced ``models`` entry alone (§2.1, §2.5, L1-3).

    Provider, model, endpoint and effort come from the YAML; so do the four
    ``prices_usd_per_mtok`` buckets. Nothing here reads a code-owned price.
    """
    model = effective.model
    pin = PinnedModel(
        provider=cast(Provider, model.provider),
        model=model.model,
        endpoint=cast(Endpoint, model.endpoint),
        effort=cast(Effort, model.effort),
    )
    prices = TokenPrices(
        input=model.input_price,
        output=model.output_price,
        cache_read=model.cache_read_price,
        cache_write=model.cache_write_price,
    )
    return pin, prices


def configured_ceilings(effective: EffectiveConfiguration) -> LoopCeilings:
    """The Attempt ceilings: the Effective Configuration's limits (§2.1, §2.5)."""
    limits = effective.workflow.limits
    return LoopCeilings(
        max_wall_clock_seconds=limits.wall_clock_minutes * 60,
        max_cost_usd=limits.cost_usd,
        max_effective_tokens=limits.tokens,
        max_tool_calls=limits.tool_calls,
    )


TrackerFactory = Callable[..., TrackerPort]
"""Builds the Worker's tracker from the startup facts (keyword arguments)."""


def github_tracker(
    *,
    owner: str,
    repo: str,
    client: GitHubClient,
    identity: Identity,
    effective: EffectiveConfiguration,
) -> TrackerPort:
    """The production tracker: the GitHub adapter (§13). No configuration
    can select the fake (C-TRK-1)."""
    return GitHubTracker(
        owner=owner,
        repo=repo,
        client=client,
        agent_login=identity.login,
        agent_id=identity.account_id,
        selection_label=effective.workflow.label,
        ready_for_human=effective.target.ready_for_human,
    )


def _refuse(message: str, *, phase: str, started: float) -> int:
    """A startup refusal: non-zero, no Attempt, no GitHub write (§3, P-WRK-3).

    Names the startup phase that was in progress so an early refusal shows
    where startup stopped; the message itself carries no credential values,
    only names (``token_env``, ``api_key_env``) and the redaction boundary
    of §18 applies.
    """
    elapsed = time.monotonic() - started
    print(
        f"run: startup refused at {phase} after {elapsed:.2f}s: {message}",
        file=sys.stderr,
    )
    return 1


def _log_startup_phase_started(phase: str) -> None:
    """Announce the startup phase now running, before its slow work (§3)."""
    print(f"run: startup: {phase} started", file=sys.stderr)


def _log_startup_phase_ok(phase: str) -> None:
    """Announce a startup phase's successful completion (§3)."""
    print(f"run: startup: {phase} ok", file=sys.stderr)


def _log_startup_phase_skipped(phase: str, detail: str) -> None:
    """Announce startup work with nothing to do, distinct from success (§3)."""
    print(f"run: startup: {phase} skipped ({detail})", file=sys.stderr)


def _record_startup_halt(
    store: OperatorStore | None,
    live: LiveView | None,
    *,
    reason: str,
    detail: str,
    registry: object | None = None,
) -> None:
    """Record the durable Halt and its ``halted`` event before exiting (#250).

    Best-effort: a halt write that itself fails never masks the refusal it
    follows. ``store`` is the operator store (same SQLite file); ``live``
    supplies nothing beyond its existence here — the event is tagged from
    the instance row the store already holds.
    """
    if store is None:
        return
    try:
        from coding_agent.runtime.redaction import SecretRegistry

        reg = registry if isinstance(registry, SecretRegistry) else None
        store.record_halt(reason=reason, detail=detail, registry=reg)
    except Exception as exc:
        print(f"run: could not record halt ({exc})", file=sys.stderr)


def _emit_startup_event(
    store: OperatorStore,
    *,
    event_type: str,
    text: str,
    registry: object | None = None,
) -> None:
    """Emit one startup Contract Event through the store's connection (#250)."""
    try:
        import sqlite3 as _sqlite

        from coding_agent.operator.events import emit_contract_event
        from coding_agent.runtime.redaction import SecretRegistry

        reg = registry if isinstance(registry, SecretRegistry) else None
        with store._lock:  # noqa: SLF001 - same package, single producer
            try:
                row = store._connection.execute(  # noqa: SLF001
                    "SELECT process_started_at FROM operator_instance WHERE id = 1"
                ).fetchone()
                process_started = str(row[0]) if row is not None and row[0] else ""
            except _sqlite.Error:
                return
            if not process_started:
                return
            emit_contract_event(
                store._connection,  # noqa: SLF001
                process_started_at=process_started,
                event_type=event_type,
                text=text,
                registry=reg,
            )
            store._connection.commit()  # noqa: SLF001
    except Exception as exc:
        print(f"run: could not emit {event_type} ({exc})", file=sys.stderr)


def run_worker(
    *,
    config: Path,
    workflow: str | None,
    state_dir: Path,
    skills_home: Path,
    skill_bundle: SkillBundleCheck,
    toolchain_matrix_path: Path | None = None,
    tracker_factory: TrackerFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_sweeps: int | None = None,
    max_attempts: int | None = None,
) -> int:
    """Start the Worker in contract order, then run its poll loop (§3).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §3. Every startup
    step runs before the Run Ledger opens, so a refusal leaves no Attempt
    and performs no GitHub write (P-WRK-3); a restart after a Credential
    Failure fails the GitHub checks and refuses the same way (L3-ERR-8).
    SIGTERM handling is installed first so the process observes the signal
    even as PID 1 in the image (§3, §11, P-CAN-3, REC-SIG-1).

    MVP row ids: P-WRK-1, P-WRK-2, P-WRK-3, P-WRK-4, L3-ERR-8, W-CFG-14,
    P-CAN-3, REC-SIG-1, REC-CFG-2, REC-INT-1, REC-INT-2, REC-INT-3,
    REC-INT-4, REC-INT-5, REC-INT-6, P-REC-1, P-REC-3, P-REC-4.

    ``tracker_factory``, ``sleep``, ``max_sweeps`` and ``max_attempts`` are
    test seams: production passes none of them and the loop runs until a
    halt.
    """
    from coding_agent.runtime.shutdown import install_shutdown_handlers

    startup_started = time.monotonic()
    print(
        f"run: startup started (config {config}, workflow {workflow!r})",
        file=sys.stderr,
    )
    install_shutdown_handlers()
    # Step 1: the Workflow Configuration, validated the way ``agent config
    # validate`` does, and resolved once into the Effective Configuration
    # every claimed Attempt is pinned to (§2.4, §2.5, W-PIN-1).
    _log_startup_phase_started("configuration")
    try:
        cfg = load_workflow_config(config, workflow_id=workflow)
        effective = resolve_effective_configuration(cfg, workflow)
    except WorkflowConfigError as exc:
        return _refuse(f"{config}: {exc}", phase="configuration", started=startup_started)
    print(
        "run: SIGTERM handling installed "
        f"(shutdown_grace_seconds={effective.worker.shutdown_grace_seconds})",
        file=sys.stderr,
    )
    owner, separator, repo = effective.target.repository.partition("/")
    if not separator or not owner or not repo:
        return _refuse(
            f"{config}: target.repository must be OWNER/REPO, "
            f"got {effective.target.repository!r}",
            phase="configuration",
            started=startup_started,
        )
    _log_startup_phase_ok("configuration")
    # The redaction registry is built from the Effective Configuration's
    # credential names right after validation (#250): startup Contract
    # Events and Halt details redact through it from the first event on.
    try:
        _early_registry = SecretRegistry.from_environment(
            credential_env_vars(
                effective.target.token_env, effective.model.api_key_env
            )
        )
    except Exception:
        from coding_agent.runtime.redaction import SecretRegistry as _SR

        _early_registry = _SR({})
    # Step 2: the exclusive volume lock on the state volume, taken right
    # after validating the configuration (§3, W-CFG-14). A second process
    # using the same state directory is refused here, before any GitHub
    # call. The lock is held until the Worker exits.
    _log_startup_phase_started("volume-lock")
    try:
        volume_lock = acquire_volume_lock(state_dir)
    except VolumeLockHeld as exc:
        return _refuse(str(exc), phase="volume-lock", started=startup_started)
    except OSError as exc:
        return _refuse(
            f"cannot take the exclusive lock on {state_dir}: {exc}",
            phase="volume-lock",
            started=startup_started,
        )
    _log_startup_phase_ok("volume-lock")
    # Step 2b: the operator store and control listener (adoption step 4,
    # #248). The store opens right after the volume lock, in the ledger's
    # SQLite file with separate tables and its own connection; the
    # listener starts right after the store opens. Both stop only when the
    # process exits, so a SIGTERM shutdown keeps serving reads until then.
    operator_store: OperatorStore | None = None
    operator_listener: OperatorListener | None = None
    try:
        from coding_agent.operator.events import (
            EVENT_PROCESS_STARTED,
            EVENT_READY,
            EVENT_STARTUP_CHECK,
        )

        operator_started_at = datetime.now(timezone.utc)
        ledger_path = state_dir / "run-ledger.sqlite3"
        operator_store = OperatorStore(ledger_path)
        operator_store.write_instance_metadata(
            target_repository=effective.target.repository,
            backend=OPERATOR_BACKEND,
            version=agent_version(),
            process_started_at=operator_started_at,
        )
        live = LiveView(process_started_at=operator_started_at)
        _emit_startup_event(
            operator_store,
            event_type=EVENT_PROCESS_STARTED,
            text=f"worker started on {effective.target.repository}",
            registry=_early_registry,
        )
        operator_listener = OperatorListener(
            ledger_path=ledger_path,
            socket_path=worker_socket_path(state_dir),
            live=live,
        )
        try:
            operator_listener.start()
        except OSError as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"cannot bind the operator socket: {exc}",
                registry=_early_registry,
            )
            operator_store.close()
            operator_store = None
            operator_listener = None
            return _refuse(
                f"cannot bind the operator socket: {exc}",
                phase="operator-listener",
                started=startup_started,
            )
        _log_startup_phase_started("toolchain-matrix")
        resolved_matrix_path = resolve_toolchain_matrix_path(toolchain_matrix_path)
        live.set_startup_check("toolchain-matrix")
        _emit_startup_event(
            operator_store,
            event_type=EVENT_STARTUP_CHECK,
            text="toolchain-matrix",
            registry=_early_registry,
        )
        try:
            matrix = load_supported_toolchain_matrix(resolved_matrix_path)
        except (
            OSError,
            json.JSONDecodeError,
            MalformedToolchainMatrix,
            ValueError,
        ) as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=(
                    "could not load the Supported Toolchain Matrix from "
                    f"{resolved_matrix_path}: {exc}"
                ),
                registry=_early_registry,
            )
            return _refuse(
                f"could not load the Supported Toolchain Matrix from "
                f"{resolved_matrix_path}: {exc}",
                phase="toolchain-matrix",
                started=startup_started,
            )
        _log_startup_phase_ok("toolchain-matrix")

        # Step 3: the Skill Bundle the steps activate.
        _log_startup_phase_started("skill-bundle")
        live.set_startup_check("skill-bundle")
        _emit_startup_event(
            operator_store,
            event_type=EVENT_STARTUP_CHECK,
            text="skill-bundle",
            registry=_early_registry,
        )
        try:
            bundle_report = verify_installed_skill_bundle(skill_bundle)
        except (OSError, json.JSONDecodeError) as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"could not read the Skill Bundle reports: {exc}",
                registry=_early_registry,
            )
            return _refuse(
                f"could not read the Skill Bundle reports: {exc}",
                phase="skill-bundle",
                started=startup_started,
            )
        if not bundle_report.ok:
            failed = "; ".join(
                f"{result.name}: {result.detail}"
                for result in bundle_report.results
                if not result.passed
            )
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"Skill Bundle verification failed: {failed}",
                registry=_early_registry,
            )
            return _refuse(
                f"Skill Bundle verification failed: {failed}",
                phase="skill-bundle",
                started=startup_started,
            )
        _log_startup_phase_ok("skill-bundle")

        # Step 4: the non-mutating GitHub checks of §18.
        _log_startup_phase_started("github-checks")
        live.set_startup_check("github-checks")
        _emit_startup_event(
            operator_store,
            event_type=EVENT_STARTUP_CHECK,
            text="github-checks",
            registry=_early_registry,
        )
        token_env = effective.target.token_env
        token = os.environ.get(token_env)
        if not token:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"{config}: target.token_env: {token_env} is not set",
                registry=_early_registry,
            )
            return _refuse(
                f"{config}: target.token_env: {token_env} is not set",
                phase="github-checks",
                started=startup_started,
            )
        client = GitHubClient(token)
        try:
            startup = run_startup_checks(client, token, owner, repo)
        except (TokenRejected, StartupCheckFailed, requests.RequestException) as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"GitHub startup check failed: {exc}",
                registry=_early_registry,
            )
            return _refuse(
                f"GitHub startup check failed: {exc}",
                phase="github-checks",
                started=startup_started,
            )
        if not startup.push_allowed:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"permissions.push is not true on {owner}/{repo}",
                registry=_early_registry,
            )
            return _refuse(
                f"permissions.push is not true on {owner}/{repo}",
                phase="github-checks",
                started=startup_started,
            )
        identity = startup.identity
        _log_startup_phase_ok("github-checks")

        # Step 5: the Provider Capability Assertion for the Pinned Model, priced
        # by the Effective Configuration alone (ADR 0009, L1-3, L1-4).
        _log_startup_phase_started("provider-capability")
        live.set_startup_check("provider-capability")
        _emit_startup_event(
            operator_store,
            event_type=EVENT_STARTUP_CHECK,
            text="provider-capability",
            registry=_early_registry,
        )
        pin, price = configured_pin(effective)
        price_table: PriceTable = {pin.key: price}
        api_key_env = effective.model.api_key_env
        api_key = os.environ.get(api_key_env)
        if not api_key:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=(
                    f"{config}: models.{effective.model.id}.api_key_env: "
                    f"{api_key_env} is not set"
                ),
                registry=_early_registry,
            )
            return _refuse(
                f"{config}: models.{effective.model.id}.api_key_env: "
                f"{api_key_env} is not set",
                phase="provider-capability",
                started=startup_started,
            )
        model = build_chat_model(pin, api_key=api_key)
        try:
            assert_provider_capability(pin, model, price_table)
        except ProviderCapabilityRefused as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"provider-capability-refused: {exc}",
                registry=_early_registry,
            )
            return _refuse(
                f"provider-capability-refused: {exc}",
                phase="provider-capability",
                started=startup_started,
            )
        context_window = context_window_for(pin, DEFAULT_CONTEXT_WINDOWS)
        if context_window is None:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=(
                    "no context-window configuration for pinned model"
                    f" {pin.key!r}"
                ),
                registry=_early_registry,
            )
            return _refuse(
                f"no context-window configuration for pinned model {pin.key!r}",
                phase="provider-capability",
                started=startup_started,
            )
        _log_startup_phase_ok("provider-capability")
        live.set_startup_check(None)

        # Step 6: the Run Ledger. An unfinished Attempt whose Effective
        # Configuration is missing or unreadable refuses startup here
        # (REC-CFG-2): before the tracker exists, so no GitHub write is
        # possible. Step 7, startup recovery, runs inside the poll loop:
        # every sweep settles unfinished Attempts (§17) before any
        # selection, so recovery precedes the first Claim and a failed
        # recovery write is retried on the next poll cycle (REC-WRK-1).
        _log_startup_phase_started("run-ledger")
        ceilings = configured_ceilings(effective)
        registry = _early_registry
        ledger = RunLedger(
            state_dir / "run-ledger.sqlite3", ceilings=ceilings, registry=registry
        )
        try:
            unfinished = validate_recovery_configs(ledger)
        except RecoveryRefused as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_RECOVERY_REFUSED,
                detail=str(exc),
                registry=registry,
            )
            checkpoint_or_ignore(ledger)
            ledger.close()
            return _refuse(str(exc), phase="run-ledger", started=startup_started)
        _log_startup_phase_ok("run-ledger")
        _log_startup_phase_started("startup-recovery")
        if unfinished:
            print(
                "run: the Run Ledger holds unfinished Attempts: "
                + ", ".join(attempt.attempt_id for attempt in unfinished),
                file=sys.stderr,
            )
            live.set_recovering(True)
            _log_startup_phase_ok("startup-recovery")
        else:
            # Nothing to settle: readiness is reached before the first
            # sweep, with the ``ready`` event (#250). Where unfinished
            # Attempts remain, the poll loop settles them first and marks
            # readiness only after recovery.
            live.set_recovering(False)
            live.set_ready()
            _emit_startup_event(
                operator_store,
                event_type=EVENT_READY,
                text="ready",
                registry=registry,
            )
            _log_startup_phase_skipped(
                "startup-recovery", "no unfinished Attempts in the Run Ledger"
            )

        make_tracker = (
            tracker_factory if tracker_factory is not None else github_tracker
        )
        tracker = make_tracker(
            owner=owner,
            repo=repo,
            client=client,
            identity=identity,
            effective=effective,
        )
        record = effective_configuration_record(effective)
        steps = tuple(
            WorkflowStep(
                step_id=step.id, skill=step.skill, prompt_bytes=step.prompt_content
            )
            for step in effective.workflow.steps
        )
        canonical_remote = target_repository_url(owner, repo)
        mirror_dir = state_dir / "mirrors" / owner / f"{repo}.git"
        workspace_dir = state_dir / "workspaces" / owner / repo
        skills_dir = skills_home / ".agents" / "skills"
        command_runner = CredentialStrippedCommandRunner(
            credential_env_vars(token_env, api_key_env)
        )
        uninstall_streams = install_stream_redaction(registry)

        def run_one_attempt(claimed: ClaimedAttempt) -> AttemptResult:
            """Run one claimed Attempt to its recorded Terminal Outcome (§6).

            Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (every
            registered secret passes through the one redaction registry; git
            authenticates through a credential helper while the remote URL stays
            the canonical credential-free form).

            MVP row ids: L3-ERR-11, L3-ERR-12.
            """
            with temporary_git_credential_helper(token) as helper:
                ensure_mirror(
                    mirror_dir, canonical_remote, redact=registry, helper=helper
                )
                workspace = checkout_workspace(
                    mirror_dir,
                    workspace_dir,
                    origin_url=canonical_remote,
                    branch=effective.target.base_branch,
                )
                evidence_dir = (
                    state_dir
                    / "evidence"
                    / owner
                    / repo
                    / str(claimed.issue_number)
                    / str(claimed.attempt_number)
                )
                evidence_dir.mkdir(parents=True, exist_ok=True)
                transcript = TranscriptWriter(
                    evidence_dir / TRANSCRIPT_FILE_NAME,
                    registry,
                )
                result = run_attempt(
                    issue_number=claimed.issue_number,
                    attempt_number=claimed.attempt_number,
                    tracker=tracker,
                    ledger=ledger,
                    model=TranscriptModel(model, transcript),
                    workspace=workspace.path,
                    effective_configuration=record,
                    skills_dir=skills_dir,
                    toolchain_matrix=matrix,
                    command_runner=command_runner,
                    ceilings=ceilings,
                    price=price,
                    evidence_dir=evidence_dir,
                    result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
                    context_window=context_window,
                    cache_breakpoints=(pin.provider == "anthropic"),
                    remote_url=canonical_remote,
                    agent_identity=identity,
                    review_redact=registry,
                    steps=steps,
                    registry=registry,
                    git_helper=helper,
                )
                print(
                    f"run: {result.outcome} ({result.reason}); "
                    f"attempt={result.attempt_id}"
                )
                print(f"run: transcript {transcript.path}")
                return result

        claim_config = ClaimConfig(
            selection_label=effective.workflow.label,
            ready_for_human=effective.target.ready_for_human,
            base_branch=effective.target.base_branch,
            require_all=effective.workflow.require_all,
            require_any=effective.workflow.require_any,
            exclude=effective.workflow.exclude,
            agent_login=identity.login,
            agent_identity_id=identity.account_id,
            effective_configuration=record,
        )
        startup_elapsed = time.monotonic() - startup_started
        print(
            f"run: startup complete in {startup_elapsed:.2f}s",
            file=sys.stderr,
        )
        print(
            f"run: worker started on {owner}/{repo} as {identity.login} "
            f"(workflow {effective.workflow.id!r}, digest {effective.digest})"
        )
        def remote_branch_sha(branch: str) -> str | None:
            """Resolve the remote branch for recorded-outcome recovery (§13).

            The Delivery Intent's branch check needs the remote sha; the
            credential authenticates through a helper while the remote URL
            stays canonical (contract §18).
            """
            with temporary_git_credential_helper(token) as helper:
                return remote_ref_sha(
                    canonical_remote, branch, redact=registry, helper=helper
                )

        # Startup recovery settles an interrupted Attempt from the state
        # volume's checkout before any selection (§17, REC-INT-1 …
        # REC-INT-6): the same shared finalization the live SIGTERM path
        # calls, with its own short-lived credential helper for the
        # loop's git reads and pushes (the per-Attempt helper inside
        # ``run_one_attempt`` covers the Attempt graph itself).
        try:
            with temporary_git_credential_helper(token) as recovery_helper:
                return run_worker_loop(
                    tracker=tracker,
                    ledger=ledger,
                    loop_config=WorkerLoopConfig(
                        claim_config=claim_config,
                        poll_interval_seconds=(
                            effective.worker.poll_interval_seconds
                        ),
                        live=live,
                        interrupted_recovery=InterruptedRecovery(
                            workspace_path=workspace_dir,
                            remote_url=canonical_remote,
                            evidence_root=(
                                state_dir / "evidence" / owner / repo
                            ),
                            redact=registry,
                            helper=recovery_helper,
                        ),
                    ),
                    run_one_attempt=run_one_attempt,
                    sleep=sleep,
                    max_sweeps=max_sweeps,
                    max_attempts=max_attempts,
                    remote_branch_sha=remote_branch_sha,
                )
        finally:
            uninstall_streams()
            checkpoint_or_ignore(ledger)
            ledger.close()
    finally:
        if operator_listener is not None:
            operator_listener.stop()
        if operator_store is not None:
            operator_store.close()
        volume_lock.release()
