"""``agent run``: the Worker Supervisor's startup and its production wiring.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order — validate the Workflow Configuration, take the exclusive volume lock,
open the operator store, verify the Skill Bundle, run the non-mutating
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

Matrix row ids: CFG-38, CFG-42, REC-11, OPC-7; and the retained lifecycle
rows (see the acceptance matrix).

The Supervisor is plain Python, not a graph, and it never writes to GitHub
itself: the Claim (``inputs.claim``) and the Workflow Steps own every write.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.commit_identity import noreply_author
from coding_agent.identity.startup import (
    Identity,
    StartupCheckFailed,
    run_startup_checks,
)
from coding_agent.identity.token import TokenRejected
from coding_agent.inputs.authorisation import IdentityMode, detect_identity_mode
from coding_agent.inputs.claim import ClaimConfig
from coding_agent.inputs.github import GitHubStepTracker, StepTrackerError
from coding_agent.inputs.numbering import attempt_branch
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger
from coding_agent.operator.listener import OperatorListener
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import worker_socket_path
from coding_agent.operator.store import OPERATOR_BACKEND, OperatorStore, agent_version
from coding_agent.profile.services import SocketServiceProber
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    load_supported_toolchain_matrix,
    resolve_toolchain_matrix_path,
)
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    assert_provider_capability,
)
from coding_agent.provider.config import policy_for_pin
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
    GitFailure,
    checkout_workspace,
    ensure_mirror,
    head_revision,
    target_repository_url,
)
from coding_agent.runtime.graph.agent_step import AgentStepDeps, StepAttemptFacts
from coding_agent.runtime.graph.workflow_graph import WorkflowResult, run_workflow
from coding_agent.operator.step_projection import describe_limit_fact
from coding_agent.runtime.redaction import (
    SecretRegistry,
    install_stream_redaction,
    temporary_git_credential_helper,
)
from coding_agent.runtime.step_finalisation import (
    count_uncommitted_files,
    perform_terminal_writes,
    render_terminal_comment,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.step_limits import build_budgets
from coding_agent.runtime.step_operations import (
    LiveCommitPushGitOps,
    LocalOperationRunner,
    tree_identity,
)
from coding_agent.runtime.step_publication import PublishingOperationRunner
from coding_agent.runtime.step_recovery import (
    _landed_summaries,
    _not_performed_summaries,
)
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
from coding_agent.validate.runner import CredentialStrippedCommandRunner
from coding_agent.workflow.v2_schema import V2Config, V2ModelDecl, V2Workflow
from coding_agent.workflow.v2_validate import validate_v2_file
from coding_agent.worker.run import (
    HALT_CLAIM_INTERRUPTED,
    HALT_CREDENTIAL_FAILURE,
    HALT_INFRASTRUCTURE_FAILURE,
    HALT_LEGACY_ATTEMPT,
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
    """What startup step 3 verifies the installed Skill Bundle against (§4).

    The two reports are the build-time ``agent-installer install --json``
    and ``list --json`` outputs; ``home`` is the runtime user's home the
    bundle was installed into.
    """

    install_report: Path
    list_report: Path
    home: Path
    ignore_file: Path


def verify_installed_skill_bundle(check: SkillBundleCheck) -> VerificationReport:
    """Verify the Skill Bundle the Worker will activate (startup step 3, §18).

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
    Repository never sees a credential the Worker holds.
    """
    names = [token_env, *PROVIDER_KEY_ENV_VARS]
    if api_key_env and api_key_env not in names:
        names.append(api_key_env)
    return names


def configured_pin(model: V2ModelDecl) -> tuple[PinnedModel, TokenPrices]:
    """The Pinned Model and its per-bucket prices from one ``models`` entry.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.1, §2.9.
    Provider, model, endpoint and effort come from the YAML; so do the four
    ``prices_usd_per_mtok`` buckets. Nothing here reads a code-owned price.
    """
    pin = PinnedModel(
        provider=cast(Provider, model.provider),
        model=model.model,
        endpoint=cast(Endpoint, model.endpoint),
        effort=cast(Effort, model.effort),
    )
    prices = TokenPrices(
        input=model.prices.input,
        output=model.prices.output,
        cache_read=model.prices.cache_read,
        cache_write=model.prices.cache_write,
    )
    return pin, prices


def configured_ceilings(workflow: V2Workflow) -> LoopCeilings:
    """The Attempt ceilings: the Workflow's Attempt Ceiling (§2.2, §11)."""
    return LoopCeilings(
        max_wall_clock_seconds=workflow.ceiling.wall_clock_minutes * 60,
        max_cost_usd=workflow.ceiling.cost_usd,
        max_effective_tokens=int(workflow.ceiling.tokens),
        max_tool_calls=int(workflow.ceiling.tool_calls),
    )


TrackerFactory = Callable[..., StepTrackerPort]
"""Builds the Worker's tracker from the startup facts (keyword arguments)."""


def github_tracker(
    *,
    owner: str,
    repo: str,
    client: GitHubClient,
    identity: Identity,
) -> StepTrackerPort:
    """The production tracker: the GitHub step adapter (§5, §14, §18).

    No configuration can select a fake: production always builds this one.
    """
    return GitHubStepTracker(
        owner=owner,
        repo=repo,
        client=client,
        agent_login=identity.login,
        agent_id=identity.account_id,
    )


def config_digest_for(config_path: Path) -> str:
    """The configuration digest rendered into publication bodies (§10.5–§10.7).

    The sha256 of the validated file's bytes: stable for one configuration,
    different after any operator edit.
    """
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def load_prompt_texts(config_path: Path, workflow: V2Workflow) -> dict[str, str]:
    """Read every ``prompt:`` file the Workflow's steps name (§2.3, §9.3).

    Paths resolve relative to the configuration directory and were checked
    to exist inside it by static validation; a file unreadable now refuses
    startup before any Attempt opens.
    """
    base = config_path.parent.resolve()
    texts: dict[str, str] = {}
    for step in workflow.steps.values():
        if step.agent is None:
            continue
        for instruction in step.agent.instructions:
            if instruction.prompt is None or instruction.prompt in texts:
                continue
            texts[instruction.prompt] = (base / instruction.prompt).read_text(
                encoding="utf-8"
            )
    return texts


def _refuse(message: str, *, phase: str, started: float) -> int:
    """A startup refusal: non-zero, no Attempt, no GitHub write (§4).

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
    """Announce the startup phase now running, before its slow work (§4)."""
    print(f"run: startup: {phase} started", file=sys.stderr)


def _log_startup_phase_ok(phase: str) -> None:
    """Announce a startup phase's successful completion (§4)."""
    print(f"run: startup: {phase} ok", file=sys.stderr)


def _log_startup_phase_skipped(phase: str, detail: str) -> None:
    """Announce startup work with nothing to do, distinct from success (§4)."""
    print(f"run: startup: {phase} skipped ({detail})", file=sys.stderr)


def _record_startup_halt(
    store: OperatorStore | None,
    live: LiveView | None,
    *,
    reason: str,
    detail: str,
    registry: object | None = None,
) -> None:
    """Record the durable Halt and its ``halted`` event before exiting.

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
    """Emit one startup Contract Event through the store's connection."""
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
    """Start the Worker in contract order, then run its poll loop (§4).

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4. Every
    startup step runs before the Run Ledger opens, so a refusal leaves no
    Attempt and performs no GitHub write; a restart after a Credential
    Failure fails the GitHub checks and refuses the same way. A
    ``version: 1`` configuration is refused at step 1: the step runtime
    cannot run it, and the operator drains unfinished MVP Attempts with the
    last pre-change release before upgrading (§16.4). SIGTERM handling is
    installed first so the process observes the signal even as PID 1 in the
    image (§4, §12).

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
    # validate`` does (§2.7–§2.9). A ``version: 1`` file is refused here
    # with the drain instruction (§16.4): the step runtime never reads it.
    _log_startup_phase_started("configuration")
    try:
        cfg, faults = validate_v2_file(config, workflow)
    except Exception as exc:
        return _refuse(
            f"{config}: cannot validate the workflow configuration: {exc}",
            phase="configuration",
            started=startup_started,
        )
    if cfg is None:
        detail = "; ".join(
            f"{fault.path}: {fault.detail}" for fault in faults
        ) or "invalid workflow configuration"
        if _looks_like_v1(config):
            return _refuse(
                f"{config}: version 1 is not supported by this release; "
                "drain unfinished MVP Attempts with the last pre-change "
                f"release before upgrading (step-workflow contract §16.4): {detail}",
                phase="configuration",
                started=startup_started,
            )
        return _refuse(f"{config}: {detail}", phase="configuration", started=startup_started)
    if workflow is None or workflow not in cfg.workflows:
        available = ", ".join(sorted(cfg.workflows))
        return _refuse(
            f"{config}: unknown workflow {workflow!r}: available: {available}",
            phase="configuration",
            started=startup_started,
        )
    selected = cfg.workflows[workflow]
    print(
        "run: SIGTERM handling installed "
        f"(shutdown_grace_seconds={cfg.worker.shutdown_grace_seconds})",
        file=sys.stderr,
    )
    owner, separator, repo = cfg.target.repository.partition("/")
    if not separator or not owner or not repo:
        return _refuse(
            f"{config}: target.repository must be OWNER/REPO, "
            f"got {cfg.target.repository!r}",
            phase="configuration",
            started=startup_started,
        )
    _log_startup_phase_ok("configuration")
    # The redaction registry is built from the configuration's credential
    # names right after validation: startup Contract Events and Halt details
    # redact through it from the first event on.
    try:
        model_decl = cfg.models[selected.model]
    except KeyError:
        return _refuse(
            f"{config}: workflows.{workflow}.model: "
            f"unknown model {selected.model!r}",
            phase="configuration",
            started=startup_started,
        )
    try:
        _early_registry = SecretRegistry.from_environment(
            credential_env_vars(cfg.target.token_env, model_decl.api_key_env)
        )
    except Exception:
        from coding_agent.runtime.redaction import SecretRegistry as _SR

        _early_registry = _SR({})
    # Step 2: the exclusive volume lock on the state volume, taken right
    # after validating the configuration (§4). A second process using the
    # same state directory is refused here, before any GitHub call. The
    # lock is held until the Worker exits.
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
    # Step 2b: the operator store and control listener. The store opens
    # right after the volume lock, in the ledger's SQLite file with separate
    # tables and its own connection; the listener starts right after the
    # store opens. Both stop only when the process exits, so a SIGTERM
    # shutdown keeps serving reads until then.
    operator_store: OperatorStore | None = None
    operator_listener: OperatorListener | None = None
    try:
        from coding_agent.operator.events import (
            EVENT_PROCESS_STARTED,
            EVENT_READY,
            EVENT_STARTUP_CHECK,
        )
        from datetime import datetime, timezone

        operator_started_at = datetime.now(timezone.utc)
        ledger_path = state_dir / "run-ledger.sqlite3"
        operator_store = OperatorStore(ledger_path)
        operator_store.write_instance_metadata(
            target_repository=cfg.target.repository,
            backend=OPERATOR_BACKEND,
            version=agent_version(),
            process_started_at=operator_started_at,
        )
        live = LiveView(process_started_at=operator_started_at)
        _emit_startup_event(
            operator_store,
            event_type=EVENT_PROCESS_STARTED,
            text=f"worker started on {cfg.target.repository}",
            registry=_early_registry,
        )
        operator_listener = OperatorListener(
            ledger_path=ledger_path,
            socket_path=worker_socket_path(state_dir),
            live=live,
            registry=_early_registry,
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
        token_env = cfg.target.token_env
        token = os.environ.get(token_env)
        if not token:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"{config}: target.token-env: {token_env} is not set",
                registry=_early_registry,
            )
            return _refuse(
                f"{config}: target.token-env: {token_env} is not set",
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

        # Step 5: the Provider Capability Assertion for the Workflow's
        # Pinned Model, priced by the configuration alone (§2.9).
        _log_startup_phase_started("provider-capability")
        live.set_startup_check("provider-capability")
        _emit_startup_event(
            operator_store,
            event_type=EVENT_STARTUP_CHECK,
            text="provider-capability",
            registry=_early_registry,
        )
        pin, price = configured_pin(model_decl)
        price_table: PriceTable = {pin.key: price}
        api_key_env = model_decl.api_key_env
        api_key = os.environ.get(api_key_env)
        if not api_key:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=(
                    f"{config}: models.{model_decl.id}.api-key-env: "
                    f"{api_key_env} is not set"
                ),
                registry=_early_registry,
            )
            return _refuse(
                f"{config}: models.{model_decl.id}.api-key-env: "
                f"{api_key_env} is not set",
                phase="provider-capability",
                started=startup_started,
            )
        model = build_chat_model(pin, api_key=api_key)
        policy = policy_for_pin(pin)
        output_cap = policy.output_cap
        try:
            assert_provider_capability(pin, model, price_table, max_tokens=output_cap)
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
        context_window = policy.context_window
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
        if output_cap is None:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=(
                    "no code-owned output cap for pinned model"
                    f" {pin.key!r}"
                ),
                registry=_early_registry,
            )
            return _refuse(
                f"no code-owned output cap for pinned model {pin.key!r}",
                phase="provider-capability",
                started=startup_started,
            )
        _log_startup_phase_ok("provider-capability")
        live.set_startup_check(None)

        # Step 6: the Run Ledger. A ledger holding an unfinished legacy
        # Attempt refuses startup here (REC-11, §16.4): before the tracker
        # exists, so no GitHub write is possible. Unfinished step Attempts
        # are settled by the poll loop before the first selection (§4.1,
        # §16.1), so recovery precedes the first Claim and a failed
        # recovery write is retried on the next poll cycle.
        _log_startup_phase_started("run-ledger")
        ceilings = configured_ceilings(selected)
        registry = _early_registry
        ledger = RunLedger(
            state_dir / "run-ledger.sqlite3", ceilings=ceilings, registry=registry
        )
        try:
            from coding_agent.ledger.store import legacy_unfinished_attempts

            legacy = legacy_unfinished_attempts(ledger.unfinished_attempts())
            if legacy:
                ids = ", ".join(attempt.attempt_id for attempt in legacy)
                detail = (
                    "the state volume holds unfinished MVP Attempt(s): "
                    f"{ids}; drain them with the last pre-change release "
                    "before upgrading (step-workflow contract §16.4)"
                )
                _record_startup_halt(
                    operator_store,
                    live,
                    reason=HALT_LEGACY_ATTEMPT,
                    detail=detail,
                    registry=registry,
                )
                checkpoint_or_ignore(ledger)
                ledger.close()
                return _refuse(detail, phase="run-ledger", started=startup_started)
            unfinished = ledger.unfinished_attempts()
        except Exception as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_RECOVERY_REFUSED,
                detail=f"cannot read the Run Ledger: {exc}",
                registry=registry,
            )
            checkpoint_or_ignore(ledger)
            ledger.close()
            return _refuse(
                f"cannot read the Run Ledger: {exc}",
                phase="run-ledger",
                started=startup_started,
            )
        try:
            prompt_texts = load_prompt_texts(config, selected)
        except OSError as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"could not read a prompt file: {exc}",
                registry=registry,
            )
            checkpoint_or_ignore(ledger)
            ledger.close()
            return _refuse(
                f"could not read a prompt file: {exc}",
                phase="run-ledger",
                started=startup_started,
            )
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
            # sweep, with the ``ready`` event. Where unfinished Attempts
            # remain, the poll loop settles them first and marks readiness
            # only after recovery.
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
        )
        try:
            owner_id = tracker.fetch_repository_owner_id()
            identity_mode = detect_identity_mode(identity.account_id, owner_id)
        except Exception as exc:
            _record_startup_halt(
                operator_store,
                live,
                reason=HALT_STARTUP_CHECK_REFUSED,
                detail=f"could not resolve the repository owner: {exc}",
                registry=registry,
            )
            checkpoint_or_ignore(ledger)
            ledger.close()
            return _refuse(
                f"could not resolve the repository owner: {exc}",
                phase="github-checks",
                started=startup_started,
            )
        canonical_remote = target_repository_url(owner, repo)
        mirror_dir = state_dir / "mirrors" / owner / f"{repo}.git"
        workspace_dir = state_dir / "workspaces" / owner / repo
        skills_dir = skills_home / ".agents" / "skills"
        command_runner = CredentialStrippedCommandRunner(
            credential_env_vars(token_env, api_key_env)
        )
        uninstall_streams = install_stream_redaction(registry)
        author_name, author_email = noreply_author(identity)
        digest = config_digest_for(config)
        input_type = cast(InputType, selected.input.type)
        merge_resolver = _merge_base_resolver(mirror_dir)

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
            with temporary_git_credential_helper(token) as helper:
                ensure_mirror(
                    mirror_dir, canonical_remote, redact=registry, helper=helper
                )
                workspace = checkout_workspace(
                    mirror_dir,
                    workspace_dir,
                    origin_url=canonical_remote,
                    branch=cfg.target.base_branch,
                )
                evidence_dir = (
                    state_dir
                    / "evidence"
                    / owner
                    / repo
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
                    workflow_id=selected.id,
                    input_type=pin_facts.input_type,
                    input_number=claimed.input_number,
                    title=pin_facts.title,
                    body=pin_facts.body,
                    base_revision=pin_facts.base_revision,
                    current_revision=pin_facts.base_revision,
                    base_branch=pin_facts.base_branch or cfg.target.base_branch,
                    head_branch=pin_facts.head_branch,
                    input_head=pin_facts.input_head,
                )
                attempt_budgets = build_budgets(
                    selected,
                    prices=price,
                    output_cap=output_cap or 0,
                    attempt_id=claimed.attempt_id,
                )
                deps = AgentStepDeps(
                    attempt=facts,
                    model=TranscriptModel(model, transcript),
                    workspace=workspace.path,
                    tree_id=tree_identity(workspace.path),
                    skills_dir=skills_dir,
                    prompt_texts=prompt_texts,
                    ledger=ledger,
                    budgets=attempt_budgets,
                    prices=price,
                    output_cap=output_cap,
                    context_window_threshold=context_window.threshold_tokens,
                )
                local_runner = LocalOperationRunner(
                    workspace=workspace.path,
                    start_commit=head_revision(workspace.path),
                    matrix=matrix,
                    service_env={},
                    prober=SocketServiceProber(),
                    runner=command_runner,
                    evidence_dir=evidence_dir,
                    author_name=author_name,
                    author_email=author_email,
                    attempt_branch=attempt_branch(
                        selected.id,
                        claimed.input_number,
                        claimed.n,
                        pin_facts.title,
                    ),
                    input_type=str(pin_facts.input_type),
                    head_branch=pin_facts.head_branch,
                    expected_head=pin_facts.input_head,
                    input_number=claimed.input_number,
                    git=LiveCommitPushGitOps(canonical_remote),
                    remote_url=canonical_remote,
                )
                operation_runner = PublishingOperationRunner(
                    local=local_runner,
                    tracker=tracker,
                    workflow_id=selected.id,
                    config_digest=digest,
                    issue_title=pin_facts.title,
                    base_branch=cfg.target.base_branch,
                    base_revision=pin_facts.base_revision,
                )
                result = run_workflow(
                    workflow=selected,
                    attempt_id=claimed.attempt_id,
                    ledger=ledger,
                    agent_deps=deps,
                    budgets=attempt_budgets,
                    operation_runner=operation_runner,
                )
                _finalise_live_attempt(
                    tracker=tracker,
                    ledger=ledger,
                    result=result,
                    number=claimed.input_number,
                    input_type=pin_facts.input_type,
                    selection_label=selected.input.label,
                    end_labels=tuple(selected.outcomes.get(result.outcome, ())),
                    workspace_path=workspace.path,
                )
                print(
                    f"run: {result.outcome} ({result.reason}); "
                    f"attempt={result.attempt_id}"
                )
                print(f"run: transcript {transcript.path}")
                return result

        claim_config = ClaimConfig(
            workflow_id=selected.id,
            input_type=input_type,
            selection_label=selected.input.label,
            require_all=tuple(selected.input.require_all),
            require_any=tuple(selected.input.require_any),
            exclude=tuple(selected.input.exclude),
            drafts=selected.input.drafts or "exclude",
            skip_open_agent_pr=bool(selected.input.skip_open_agent_pr),
            base_branch=cfg.target.base_branch,
            agent_login=identity.login,
            agent_id=identity.account_id,
            effective_configuration=_pinned_configuration(cfg, selected),
        )
        if operator_listener is not None:
            # The ``next issue N`` eligibility read gets its own tracker
            # instance — never the Worker thread's. A fresh client per
            # commit keeps even the transport separate.
            def _listener_tracker() -> StepTrackerPort:
                return make_tracker(
                    owner=owner,
                    repo=repo,
                    client=GitHubClient(token),
                    identity=identity,
                )

            operator_listener.configure_next_issue(
                claim_config, _listener_tracker, ledger, identity_mode
            )
            operator_listener.configure_input_type(selected.input.type)
        startup_elapsed = time.monotonic() - startup_started
        print(
            f"run: startup complete in {startup_elapsed:.2f}s",
            file=sys.stderr,
        )
        print(
            f"run: worker started on {owner}/{repo} as {identity.login} "
            f"(workflow {selected.id!r})"
        )

        # The poll loop settles unfinished Attempts before the first
        # selection (§4.1, §16.1); ``run_one_attempt`` covers the Workflow
        # run itself.
        try:
            return run_worker_loop(
                tracker=tracker,
                ledger=ledger,
                loop_config=WorkerLoopConfig(
                    claim_config=claim_config,
                    workflow=selected,
                    poll_interval_seconds=cfg.worker.poll_interval_seconds,
                    live=live,
                    merge_base_resolver=merge_resolver,
                ),
                run_one_attempt=run_one_attempt,
                sleep=sleep,
                max_sweeps=max_sweeps,
                max_attempts=max_attempts,
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


def _looks_like_v1(config_path: Path) -> bool:
    """Whether the file declares ``version: 1`` (for the drain note, §16.4)."""
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("version") == 1


def _pinned_configuration(cfg: V2Config, workflow: V2Workflow) -> dict[str, object]:
    """The configuration record the Claim pins on every Attempt (§2.10).

    The Workflow and Pinned Model identity the Attempt runs under; the full
    file stays operator-owned on disk, never in the ledger.
    """
    return {
        "workflow": workflow.id,
        "model": workflow.model,
        "input_type": workflow.input.type,
        "selection_label": workflow.input.label,
        "target_repository": cfg.target.repository,
        "base_branch": cfg.target.base_branch,
    }


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
    end_labels: tuple[str, ...],
    workspace_path: Path,
) -> None:
    """Publish the live Attempt's terminal writes after the Workflow Ends.

    The Workflow End node records the outcome without a comment body (the
    first record wins, REC-13), so the Supervisor renders the terminal
    comment, supersedes the row with its final bytes before any tracker
    write, performs the terminal writes, and marks the Attempt finished
    (§15.2, §15.3).
    """
    limit = ""
    try:
        for record in ledger.list_step_executions(result.attempt_id):
            if record.limit_fact:
                limit = describe_limit_fact(record.limit_fact)
                break
    except Exception:
        pass
    try:
        uncommitted = count_uncommitted_files(workspace_path)
    except Exception:
        uncommitted = 0
    comment = render_terminal_comment(
        attempt_id=result.attempt_id,
        outcome=result.outcome,
        ending_step=result.ending_step,
        step_result=result.step_result,
        workflow_end=result.workflow_end,
        limit_fact=limit,
        landed=_landed_summaries(ledger, result.attempt_id),
        not_performed=_not_performed_summaries(ledger, result.attempt_id),
        uncommitted_count=uncommitted,
    )
    stored = ledger.get_outcome(result.attempt_id)
    ledger.update_outcome_for_publish(
        result.attempt_id,
        outcome=result.outcome,
        reason=result.reason,
        pull_request_body=stored.pull_request_body if stored is not None else "",
        terminal_comment_body=comment,
        writes=list(terminal_writes_for_outcome()),
    )
    perform_terminal_writes(
        tracker=tracker,
        ledger=ledger,
        attempt_id=result.attempt_id,
        number=number,
        input_type=input_type,
        selection_label=selection_label,
        end_labels=end_labels,
        comment_body=comment,
    )
    ledger.set_attempt_state(result.attempt_id, "finished")
