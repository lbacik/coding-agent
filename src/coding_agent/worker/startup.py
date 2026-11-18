"""Worker startup: the ordered phases behind one call, one refusal path.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order — validate the Workflow Configuration, take the exclusive volume lock,
open the operator store, load the Toolchain Matrix, verify the Skill Bundle,
check the selected secrets' presence, run the non-mutating GitHub checks of
§18, run the Provider Capability Assertion, open the Run Ledger, then settle
and finalise every unfinished Attempt; a refusal at any step exits non-zero
with no Attempt opened and no GitHub write; startup recovery refuses on a
legacy Attempt). Only ``version: 3`` runs: a ``version: 2`` file is refused
at step 1 with a migration diagnostic, and a ``version: 1`` file with the
drain instruction (§16.4).

Every startup step runs here, in the same order and behind the same
``run: startup: <phase> started|ok|skipped (...)`` stderr lines as before.
Each step signals a refusal by raising :class:`StartupRefused`; :func:`start`
catches it in exactly one place, records the Halt where the operator store
is already open, unwinds every open resource in reverse order, prints the
existing refusal line once and returns a :class:`StartupRefusal`. No other
code in startup records a Halt or prints the refusal line.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import (
    Identity,
    StartupCheckFailed,
    run_startup_checks as _real_run_startup_checks,
)
from coding_agent.identity.token import TokenRejected
from coding_agent.inputs.authorisation import IdentityMode, detect_identity_mode
from coding_agent.inputs.claim import ClaimConfig
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger as _RealRunLedger
from coding_agent.operator.listener import OperatorListener
from coding_agent.operator.live import LiveView
from coding_agent.operator.paths import worker_socket_path
from coding_agent.operator.store import OPERATOR_BACKEND, OperatorStore, agent_version
from coding_agent.profile.toolchain import (
    MalformedToolchainMatrix,
    SupportedToolchainMatrix,
    load_supported_toolchain_matrix,
    resolve_toolchain_matrix_path,
)
from coding_agent.provider.capability import (
    ProviderCapabilityRefused,
    assert_provider_capability as _real_assert_provider_capability,
)
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import (
    Effort,
    Endpoint,
    GatewayKind,
    PinnedModel,
    Provider,
    build_chat_model as _real_build_chat_model,
    resolve_gateway_url as _real_resolve_gateway_url,
)
from coding_agent.provider.price_table import PriceTable, TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.git import target_repository_url as _real_target_repository_url
from coding_agent.runtime.redaction import SecretRegistry, install_stream_redaction
from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    VerifiedRunInputs,
    verify_run_inputs as _real_verify_run_inputs,
)
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.validate.runner import CredentialStrippedCommandRunner
from coding_agent.workflow.effective import (
    build_effective_configuration,
    prompt_texts_from_run_inputs,
    skill_names_for_workflow,
)
from coding_agent.workflow.schema import ModelDecl, Workflow, WorkflowConfiguration
from coding_agent.workflow.configuration import load_workflow_configuration
from coding_agent.worker.run import (
    HALT_LEGACY_ATTEMPT,
    HALT_RECOVERY_REFUSED,
    HALT_STARTUP_CHECK_REFUSED,
    checkpoint_or_ignore,
)
from coding_agent.worker.volume import VolumeLockHeld, acquire_volume_lock

PROVIDER_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
"""The provider SDKs' default credential variables (§18)."""


@dataclass(frozen=True)
class SkillDeploymentCheck:
    """What startup step 3 verifies the prepared Skill Bundle against (§4, SKV3).

    The skill policy, the bundle directory holding the generated records, and
    the runtime user's home whose managed store the deployment is verified
    against. Verified read-only before any secret or live check; the
    activation registry is then built from the same verified bytes.
    """

    policy: Path
    bundle_dir: Path
    home: Path


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


def configured_pin(
    model: ModelDecl, *, env: Mapping[str, str] | None = None
) -> tuple[PinnedModel, TokenPrices]:
    """The Pinned Model and its per-bucket prices from one ``models`` entry.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.1, §2.9,
    §4.1. Provider (or gateway kind), model, endpoint and effort come from the
    YAML; so do the four ``prices_usd_per_mtok`` buckets. A gateway pin
    carries its own output cap and context-window threshold in the pin. A
    gateway pin with ``gateway.url-env`` resolves its address from the
    environment here — once per Worker process, before the Provider
    Capability Assertion — through the same rules as a literal
    ``gateway.url``; an unset, empty or malformed value raises
    ``ValueError`` naming the variable, never the value. Nothing here reads
    a code-owned price.
    """
    if model.gateway_kind is not None:
        if model.gateway_url_env is not None:
            if model.gateway_url is not None:
                raise ValueError(
                    f"models.{model.id}.gateway: exactly one of 'url' or "
                    "'url-env' is required"
                )
            source = os.environ if env is None else env
            gateway_url = _resolve_gateway_url(model.id, model.gateway_url_env, source)
        else:
            assert model.gateway_url is not None
            gateway_url = model.gateway_url
        assert model.output_cap_tokens is not None
        assert model.context_threshold_tokens is not None
        pin = PinnedModel(
            provider=cast(GatewayKind, model.gateway_kind),
            model=model.model,
            endpoint=cast(Endpoint, model.endpoint),
            effort=cast(Effort, model.effort),
            gateway_url=gateway_url,
            output_cap_tokens=model.output_cap_tokens,
            context_threshold_tokens=model.context_threshold_tokens,
        )
    else:
        assert model.provider is not None
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


def _resolve_gateway_url(model_id: str, name: str, env: Mapping[str, str]) -> str:
    """Resolve an env-sourced gateway address, honouring the test seam.

    Production resolves through the pinned-model rules; tests patch
    ``supervisor.resolve_gateway_url`` to observe the lookup, so the lookup
    goes through the supervisor namespace where one is installed.
    """
    try:
        import coding_agent.worker.supervisor as _supervisor

        resolve = getattr(_supervisor, "resolve_gateway_url", None)
    except ImportError:
        resolve = None
    if resolve is None:
        resolve = _real_resolve_gateway_url
    return str(resolve(model_id, name, env))


def configured_ceilings(workflow: Workflow) -> LoopCeilings:
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
    from coding_agent.inputs.github import GitHubStepTracker

    return GitHubStepTracker(
        owner=owner,
        repo=repo,
        client=client,
        agent_login=identity.login,
        agent_id=identity.account_id,
    )


class StartupRefused(Exception):
    """A startup step's refusal, caught in exactly one place (§4).

    ``halt_reason`` defaults to the startup-check refusal: every refusal
    after the operator store opens records it as a durable Halt. Refusals
    before the store opens record nothing. ``halt_detail`` defaults to the
    refusal message itself.
    """

    def __init__(
        self,
        phase: str,
        message: str,
        halt_reason: str = HALT_STARTUP_CHECK_REFUSED,
        halt_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.halt_reason = halt_reason
        self.halt_detail = halt_detail if halt_detail is not None else message


@dataclass(frozen=True)
class StartupRefusal:
    """What :func:`start` returns where a startup step refused (§4)."""

    phase: str
    """The startup phase that was in progress when startup stopped."""
    message: str
    """The refusal message, carrying no credential values."""


@dataclass
class Ready:
    """Everything the poll loop and the Attempt run need (§4).

    Owns the startup :class:`ExitStack`: leaving the context closes every
    startup resource in reverse order — stream redaction first, then the Run
    Ledger checkpoint and close, then the listener stop, the store close and
    the volume-lock release — exactly as the inline ``finally`` blocks did.
    """

    config: WorkflowConfiguration
    """The validated Workflow Configuration (§2.7–§2.9)."""
    workflow: Workflow
    """The selected Workflow: input type, labels and end labels (§2.2)."""
    model_decl: ModelDecl
    """The selected Workflow's configured model entry."""
    matrix: SupportedToolchainMatrix
    """The loaded Supported Toolchain Matrix."""
    run_inputs: VerifiedRunInputs
    """The verified run inputs the Effective Configuration pins (§2.10)."""
    activation_registry: ActivationRegistry
    """The activation registry built from the same verified bytes."""
    prompt_texts: dict[str, str]
    """The selected Workflow's prompt texts decoded from the verified bytes."""
    registry: SecretRegistry
    """The redaction registry built from the configuration's secret names."""
    pin: PinnedModel
    """The Workflow's Pinned Model (§2.1, §2.9)."""
    price: TokenPrices
    """The pin's per-bucket prices from the configuration alone."""
    price_table: PriceTable
    """The single-entry price table the Capability Assertion ran under."""
    model: Any
    """The chat model the Capability Assertion passed for the pin."""
    output_cap: int | None
    """The code-owned output cap for the pinned model."""
    context_threshold_tokens: int
    """The context-window threshold for the pinned model."""
    tracker: StepTrackerPort
    """The step tracker the poll loop selects and finalises through."""
    ledger: _RealRunLedger
    """The open Run Ledger (§15)."""
    live: LiveView
    """The prepared live view the control listener reads."""
    claim_config: ClaimConfig
    """The Selection-and-Claim sweep parameters (§5, §6)."""
    identity: Identity
    """The Agent Identity the GitHub checks returned."""
    identity_mode: IdentityMode
    """The detected identity mode for the target repository."""
    state_dir: Path
    """The state volume directory holding the lock and the ledger."""
    mirror_dir: Path
    """The mirror checkout directory for the target repository."""
    workspace_dir: Path
    """The workspace checkout directory for Attempts."""
    canonical_remote: str
    """The credential-free remote URL git authenticates against."""
    token: str
    """The Agent Identity token (never logged)."""
    token_env: str
    """The configured variable holding the Agent Identity token."""
    api_key_env: str
    """The configured variable holding the selected model's API key."""
    owner: str
    """The target repository owner."""
    repo: str
    """The target repository name."""
    command_runner: CredentialStrippedCommandRunner
    """The Attempt command runner stripped of every credential variable."""
    pinned_configuration: dict[str, object]
    """The pinned Effective Configuration record the Claim pins (§2.10)."""
    _stack: ExitStack = field(repr=False)
    """The startup resources, closed in reverse order on exit."""

    def __enter__(self) -> Ready:
        """Enter the run: the resources stay open until the block exits."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close every startup resource in reverse order (§4, §17)."""
        self._stack.close()


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
    check: str | None = None,
    result: str | None = None,
    agent_version: str | None = None,
) -> None:
    """Emit one startup Contract Event through the operator store.

    Best-effort: a failed emit prints the same stderr line and never
    stops startup. Contract ``v1.0.4`` extras: ``process_started`` carries
    ``agent_version``, ``startup_check`` carries ``check`` and ``result``.
    """
    try:
        reg = registry if isinstance(registry, SecretRegistry) else None
        store.emit_startup_event(
            event_type=event_type,
            text=text,
            registry=reg,
            check=check,
            result=result,
            agent_version=agent_version,
        )
    except Exception as exc:
        print(f"run: could not emit {event_type} ({exc})", file=sys.stderr)


def start(
    *,
    config: Path,
    workflow: str | None,
    state_dir: Path,
    skill_deployment: SkillDeploymentCheck,
    toolchain_matrix_path: Path | None = None,
    tracker_factory: TrackerFactory | None = None,
) -> Ready | StartupRefusal:
    """Run Worker startup in contract order, then hand over a :class:`Ready`.

    Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4. Takes
    the same inputs ``run_worker`` takes for startup; every refusal raises
    :class:`StartupRefused` internally and is caught in exactly one place
    below, which records the Halt where the operator store is already open,
    unwinds every open resource in reverse order, prints the existing
    refusal line once and returns a :class:`StartupRefusal`.
    """
    from coding_agent.runtime.shutdown import install_shutdown_handlers

    # The test seams live on the supervisor namespace so the current
    # monkeypatches keep working until the ports land (#454).
    from coding_agent.worker import supervisor as _supervisor

    verify_run_inputs = getattr(
        _supervisor, "verify_run_inputs", _real_verify_run_inputs
    )
    run_startup_checks = getattr(
        _supervisor, "run_startup_checks", _real_run_startup_checks
    )
    build_chat_model = getattr(
        _supervisor, "build_chat_model", _real_build_chat_model
    )
    assert_provider_capability = getattr(
        _supervisor, "assert_provider_capability", _real_assert_provider_capability
    )
    RunLedger = getattr(_supervisor, "RunLedger", _RealRunLedger)
    target_repository_url = getattr(
        _supervisor, "target_repository_url", _real_target_repository_url
    )

    startup_started = time.monotonic()
    print(
        f"run: startup started (config {config}, workflow {workflow!r})",
        file=sys.stderr,
    )
    install_shutdown_handlers()
    stack = ExitStack()
    store: OperatorStore | None = None
    live: LiveView | None = None
    early_registry: SecretRegistry | None = None
    try:
        # Step 1: the Workflow Configuration, validated the way ``agent config
        # validate`` does (§2.7–§2.9, SKV3-01, SKV3-05). Only ``version: 3``
        # runs: a ``version: 2`` file is refused here with a migration
        # diagnostic before installation, live calls, Claim or GitHub writes,
        # and a ``version: 1`` file with the drain instruction (§16.4). The
        # whole file is always validated even though only one Workflow runs;
        # the whole ``skills`` declaration always counts as the bundle
        # membership (SKV3-05).
        _log_startup_phase_started("configuration")
        try:
            loaded, faults = load_workflow_configuration(config, workflow)
        except Exception as exc:
            raise StartupRefused(
                "configuration",
                f"{config}: cannot validate the workflow configuration: {exc}",
            )
        if loaded is None:
            detail = "; ".join(
                f"{fault.path}: {fault.detail}" for fault in faults
            ) or "invalid workflow configuration"
            raise StartupRefused("configuration", f"{config}: {detail}")
        cfg = loaded.config
        if workflow is None or workflow not in cfg.workflows:
            available = ", ".join(sorted(cfg.workflows))
            raise StartupRefused(
                "configuration",
                f"{config}: unknown workflow {workflow!r}: available: {available}",
            )
        selected = cfg.workflows[workflow]
        print(
            "run: SIGTERM handling installed "
            f"(shutdown_grace_seconds={cfg.worker.shutdown_grace_seconds})",
            file=sys.stderr,
        )
        owner, separator, repo = cfg.target.repository.partition("/")
        if not separator or not owner or not repo:
            raise StartupRefused(
                "configuration",
                f"{config}: target.repository must be OWNER/REPO, "
                f"got {cfg.target.repository!r}",
            )
        _log_startup_phase_ok("configuration")
        # The redaction registry is built from the configuration's credential
        # names right after validation: startup Contract Events and Halt details
        # redact through it from the first event on.
        try:
            model_decl = cfg.models[selected.model]
        except KeyError:
            raise StartupRefused(
                "configuration",
                f"{config}: workflows.{workflow}.model: "
                f"unknown model {selected.model!r}",
            )
        try:
            early_registry = SecretRegistry.from_environment(
                credential_env_vars(cfg.target.token_env, model_decl.api_key_env)
            )
        except Exception:
            early_registry = SecretRegistry({})
        # Step 2: the exclusive volume lock on the state volume, taken right
        # after validating the configuration (§4). A second process using the
        # same state directory is refused here, before any GitHub call. The
        # lock is held until the Worker exits.
        _log_startup_phase_started("volume-lock")
        try:
            volume_lock = acquire_volume_lock(state_dir)
        except VolumeLockHeld as exc:
            raise StartupRefused("volume-lock", str(exc))
        except OSError as exc:
            raise StartupRefused(
                "volume-lock",
                f"cannot take the exclusive lock on {state_dir}: {exc}",
            )
        stack.callback(volume_lock.release)
        _log_startup_phase_ok("volume-lock")
        # Step 2b: the operator store and control listener. The store opens
        # right after the volume lock, in the ledger's SQLite file with separate
        # tables and its own connection; the listener starts right after the
        # store opens. Both stop only when the process exits, so a SIGTERM
        # shutdown keeps serving reads until then.
        from coding_agent.operator.events import (
            EVENT_PROCESS_STARTED,
            EVENT_READY,
            EVENT_STARTUP_CHECK,
        )
        from datetime import datetime, timezone

        operator_started_at = datetime.now(timezone.utc)
        ledger_path = state_dir / "run-ledger.sqlite3"
        store = OperatorStore(ledger_path)
        stack.callback(store.close)
        store.write_instance_metadata(
            target_repository=cfg.target.repository,
            backend=OPERATOR_BACKEND,
            version=agent_version(),
            process_started_at=operator_started_at,
        )
        live = LiveView(process_started_at=operator_started_at)
        _emit_startup_event(
            store,
            event_type=EVENT_PROCESS_STARTED,
            text=f"worker started on {cfg.target.repository}",
            registry=early_registry,
            agent_version=agent_version(),
        )
        operator_listener = OperatorListener(
            ledger_path=ledger_path,
            socket_path=worker_socket_path(state_dir),
            live=live,
            registry=early_registry,
        )
        stack.callback(operator_listener.stop)
        try:
            operator_listener.start()
        except OSError as exc:
            raise StartupRefused(
                "operator-listener", f"cannot bind the operator socket: {exc}"
            )
        _log_startup_phase_started("toolchain-matrix")
        resolved_matrix_path = resolve_toolchain_matrix_path(toolchain_matrix_path)
        live.set_startup_check("toolchain-matrix")
        _emit_startup_event(
            store,
            event_type=EVENT_STARTUP_CHECK,
            text="toolchain-matrix",
            registry=early_registry,
            check="toolchain-matrix",
            result="started",
        )
        try:
            matrix = load_supported_toolchain_matrix(resolved_matrix_path)
        except (
            OSError,
            json.JSONDecodeError,
            MalformedToolchainMatrix,
            ValueError,
        ) as exc:
            raise StartupRefused(
                "toolchain-matrix",
                "could not load the Supported Toolchain Matrix from "
                f"{resolved_matrix_path}: {exc}",
            )
        _log_startup_phase_ok("toolchain-matrix")

        # Step 3: the Skill Bundle the steps activate. This repeats the full
        # offline deployment verification before any secret or live check,
        # then builds the activation registry from the same verified bytes.
        # The build-only prepare path is unreachable from here.
        _log_startup_phase_started("skill-bundle")
        live.set_startup_check("skill-bundle")
        _emit_startup_event(
            store,
            event_type=EVENT_STARTUP_CHECK,
            text="skill-bundle",
            registry=early_registry,
            check="skill-bundle",
            result="started",
        )
        result = verify_run_inputs(
            config=loaded,
            policy=skill_deployment.policy,
            bundle_dir=skill_deployment.bundle_dir,
            home=skill_deployment.home,
        )
        if isinstance(result, RunVerificationRefusal):
            if result.error is not None:
                detail = (
                    "could not verify the Skill Bundle deployment: "
                    f"{result.error}"
                )
            else:
                failed = "; ".join(str(fault) for fault in result.faults)
                detail = (
                    "Skill Bundle deployment verification failed: "
                    f"{failed}"
                )
            raise StartupRefused("skill-bundle", detail)
        run_inputs: VerifiedRunInputs = result
        _log_startup_phase_ok("skill-bundle")

        # Selected-secret presence: the target token and the selected
        # Workflow's model key only. A key used only by another Workflow
        # never blocks startup. This runs after the local checks and
        # before any live call, Attempt or GitHub write.
        _log_startup_phase_started("credentials")
        live.set_startup_check("credentials")
        _emit_startup_event(
            store,
            event_type=EVENT_STARTUP_CHECK,
            text="credentials",
            registry=early_registry,
            check="credentials",
            result="started",
        )
        token_env = cfg.target.token_env
        api_key_env = model_decl.api_key_env
        if not os.environ.get(token_env):
            raise StartupRefused(
                "credentials",
                f"{config}: target.token-env: {token_env} is not set",
            )
        if not os.environ.get(api_key_env):
            raise StartupRefused(
                "credentials",
                f"{config}: models.{model_decl.id}.api-key-env: "
                f"{api_key_env} is not set",
            )
        _log_startup_phase_ok("credentials")

        # Step 4: the non-mutating GitHub checks of §18.
        _log_startup_phase_started("github-checks")
        live.set_startup_check("github-checks")
        _emit_startup_event(
            store,
            event_type=EVENT_STARTUP_CHECK,
            text="github-checks",
            registry=early_registry,
            check="github-checks",
            result="started",
        )
        token_env = cfg.target.token_env
        token = os.environ.get(token_env)
        if not token:
            raise StartupRefused(
                "github-checks",
                f"{config}: target.token-env: {token_env} is not set",
            )
        client = GitHubClient(token)
        try:
            startup = run_startup_checks(client, token, owner, repo)
        except (TokenRejected, StartupCheckFailed, requests.RequestException) as exc:
            raise StartupRefused(
                "github-checks", f"GitHub startup check failed: {exc}"
            )
        if not startup.push_allowed:
            raise StartupRefused(
                "github-checks",
                f"permissions.push is not true on {owner}/{repo}",
            )
        identity = startup.identity
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
            raise StartupRefused(
                "github-checks",
                f"could not resolve the repository owner: {exc}",
            )
        _log_startup_phase_ok("github-checks")

        # Step 5: the Provider Capability Assertion for the Workflow's
        # Pinned Model, priced by the configuration alone (§2.9).
        _log_startup_phase_started("provider-capability")
        live.set_startup_check("provider-capability")
        _emit_startup_event(
            store,
            event_type=EVENT_STARTUP_CHECK,
            text="provider-capability",
            registry=early_registry,
            check="provider-capability",
            result="started",
        )
        try:
            pin, price = configured_pin(model_decl)
        except ValueError as exc:
            # An env-sourced gateway address that is unset, empty or
            # malformed (§4.1): the refusal names the variable, never the
            # refused value, which may carry userinfo.
            raise StartupRefused("provider-capability", f"{config}: {exc}")
        price_table: PriceTable = {pin.key: price}
        api_key_env = model_decl.api_key_env
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise StartupRefused(
                "provider-capability",
                f"{config}: models.{model_decl.id}.api-key-env: "
                f"{api_key_env} is not set",
            )
        model = build_chat_model(pin, api_key=api_key)
        policy = policy_for_pin(pin)
        output_cap = policy.output_cap
        try:
            assert_provider_capability(pin, model, price_table, max_tokens=output_cap)
        except ProviderCapabilityRefused as exc:
            raise StartupRefused(
                "provider-capability", f"provider-capability-refused: {exc}"
            )
        context_window = policy.context_window
        if context_window is None:
            raise StartupRefused(
                "provider-capability",
                "no context-window configuration for pinned model"
                f" {pin.key!r}",
            )
        if output_cap is None:
            raise StartupRefused(
                "provider-capability",
                "no code-owned output cap for pinned model"
                f" {pin.key!r}",
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
        assert early_registry is not None
        registry = early_registry
        ledger = RunLedger(
            state_dir / "run-ledger.sqlite3", ceilings=ceilings, registry=registry
        )
        stack.callback(ledger.close)
        stack.callback(checkpoint_or_ignore, ledger)
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
                raise StartupRefused(
                    "run-ledger", detail, halt_reason=HALT_LEGACY_ATTEMPT
                )
            unfinished = ledger.unfinished_attempts()
        except StartupRefused:
            raise
        except Exception as exc:
            raise StartupRefused(
                "run-ledger",
                f"cannot read the Run Ledger: {exc}",
                halt_reason=HALT_RECOVERY_REFUSED,
            )
        # The run inputs come from the single skill-bundle read: the prompt
        # texts decode from the verified prompt bytes, and the registry,
        # members and contents are used as verified — never re-read, never
        # copied. A prompt that changed after verification was already
        # refused by the skill-bundle phase's time-of-check re-read.
        prompt_texts = prompt_texts_from_run_inputs(run_inputs, selected)
        # The activation registry comes from the same verified bytes step 3
        # checked: static instructions resolve declared names only, and an
        # unknown name refuses before any Attempt opens. The version-2
        # identity core covers every available member, not just the static
        # texts, with exact deliveries and framing version.
        activation_registry = run_inputs.registry
        static_names = skill_names_for_workflow(selected)
        unknown_static = [
            name for name in static_names if name not in activation_registry.bodies
        ]
        if unknown_static:
            detail = f"could not read a skill file: {unknown_static[0]!r} is not in the activation registry"
            raise StartupRefused("run-ledger", detail)
        pinned_configuration = build_effective_configuration(
            run_inputs,
            workflow=selected,
            pin=pin,
            prices=price,
        )
        _log_startup_phase_ok("run-ledger")
        # The sealed-archive repair sweep: archives sealed before the
        # duplicate-``attempt_finished`` fix carry one trailing line past
        # their manifest. Truncate those back to the sealed bytes before
        # the first Claim (Intake opens no Claim until the poll loop
        # selects), best-effort: a failure repairs nothing and never
        # refuses startup or stops the sweep.
        _log_startup_phase_started("archive-repair")
        try:
            from coding_agent.operator import attempts as attempt_archives

            repair_summary = attempt_archives.sweep_sealed_archive_repairs(state_dir)
            print(
                "run: archive repair: "
                f"{repair_summary['repaired']} repaired of "
                f"{repair_summary['scanned']} archives scanned",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"run: archive repair skipped ({exc})", file=sys.stderr)
        _log_startup_phase_ok("archive-repair")
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
                store,
                event_type=EVENT_READY,
                text="ready",
                registry=registry,
            )
            _log_startup_phase_skipped(
                "startup-recovery", "no unfinished Attempts in the Run Ledger"
            )

        canonical_remote = target_repository_url(owner, repo)
        mirror_dir = state_dir / "mirrors" / owner / f"{repo}.git"
        workspace_dir = state_dir / "workspaces" / owner / repo
        command_runner = CredentialStrippedCommandRunner(
            credential_env_vars(token_env, api_key_env)
        )
        uninstall_streams = install_stream_redaction(registry)
        stack.callback(uninstall_streams)

        claim_config = ClaimConfig(
            workflow_id=selected.id,
            input_type=cast(InputType, selected.input.type),
            selection_label=selected.input.label,
            require_all=tuple(selected.input.require_all),
            require_any=tuple(selected.input.require_any),
            exclude=tuple(selected.input.exclude),
            drafts=selected.input.drafts or "exclude",
            skip_open_agent_pr=bool(selected.input.skip_open_agent_pr),
            base_branch=cfg.target.base_branch,
            agent_login=identity.login,
            agent_id=identity.account_id,
            effective_configuration=pinned_configuration,
            prompt_texts=prompt_texts,
            activation_deliveries=run_inputs.registry.bodies,
            activation_components=run_inputs.contents,
        )
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
        return Ready(
            config=cfg,
            workflow=selected,
            model_decl=model_decl,
            matrix=matrix,
            run_inputs=run_inputs,
            activation_registry=activation_registry,
            prompt_texts=prompt_texts,
            registry=registry,
            pin=pin,
            price=price,
            price_table=price_table,
            model=model,
            output_cap=output_cap,
            context_threshold_tokens=context_window.threshold_tokens,
            tracker=tracker,
            ledger=ledger,
            live=live,
            claim_config=claim_config,
            identity=identity,
            identity_mode=identity_mode,
            state_dir=state_dir,
            mirror_dir=mirror_dir,
            workspace_dir=workspace_dir,
            canonical_remote=canonical_remote,
            token=token,
            token_env=token_env,
            api_key_env=api_key_env,
            owner=owner,
            repo=repo,
            command_runner=command_runner,
            pinned_configuration=pinned_configuration,
            _stack=stack,
        )
    except StartupRefused as refused:
        if store is not None:
            _record_startup_halt(
                store,
                live,
                reason=refused.halt_reason,
                detail=refused.halt_detail,
                registry=early_registry,
            )
        stack.close()
        elapsed = time.monotonic() - startup_started
        print(
            f"run: startup refused at {refused.phase} after {elapsed:.2f}s: {refused.message}",
            file=sys.stderr,
        )
        return StartupRefusal(phase=refused.phase, message=refused.message)
    except BaseException:
        stack.close()
        raise


__all__ = [
    "PROVIDER_KEY_ENV_VARS",
    "Ready",
    "SkillDeploymentCheck",
    "StartupRefusal",
    "StartupRefused",
    "TrackerFactory",
    "checkpoint_or_ignore",
    "configured_ceilings",
    "configured_pin",
    "credential_env_vars",
    "github_tracker",
    "start",
]
