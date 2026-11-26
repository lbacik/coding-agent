"""The production Attempt run: one claimed Attempt through one Workflow run.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (the
Attempt run to its recorded Terminal Outcome) and §18 (every registered
secret passes through the one redaction registry; git authenticates
through a credential helper while the remote URL stays the canonical
credential-free form).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from coding_agent.attempt_id import AttemptId
from coding_agent.identity.commit_identity import noreply_author
from coding_agent.inputs.claim import ConfirmedClaim
from coding_agent.inputs.gates import GateResult, check_gate
from coding_agent.inputs.pinning import PinnedInput
from coding_agent.inputs.port import InputType, StepTrackerPort
from coding_agent.ledger.store import RunLedger
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import SocketServiceProber
from coding_agent.provider.observed_identity import PIN_VIOLATION_REASON
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.conversation_context import ContextWindowConfig
from coding_agent.runtime.git import (
    GitFailure,
    checkout_branch_at,
    checkout_workspace,
    ensure_mirror,
    fetch_revision,
    head_revision,
    is_ancestor,
    remote_ref_sha,
)
from coding_agent.runtime.graph.agent_step import AgentStepDeps, StepAttemptFacts
from coding_agent.runtime.graph.step_executor import ProductionStepExecutor
from coding_agent.runtime.graph.workflow_graph import (
    WorkflowResult,
    run_workflow,
)
from coding_agent.runtime.redaction import (
    GitCredentialHelper,
    Redactable,
    SecretRegistry,
    temporary_git_credential_helper,
)
from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.validate.runner import CommandRunner
from coding_agent.runtime.round_handoff import (
    HandoffDiagnostic,
    RoundHandoffOffer,
    SummaryInput,
    build_live_preparation,
    decide_handoff,
    hard_handoff_note,
    truncate_note,
)
from coding_agent.runtime.shutdown import shutdown_grace_deadline
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
from coding_agent.runtime.step_limits import AttemptBudgets, build_budgets
from coding_agent.runtime.step_recovery import (
    SHUTDOWN_REASON,
    GateCheck,
    finalise_shutdown,
)
from coding_agent.runtime.step_operations import (
    LiveCommitPushGitOps,
    LocalOperationRunner,
    tree_identity,
)
from coding_agent.runtime.step_publication import (
    DocumentResolutionError,
    PublishingOperationRunner,
    resolve_document,
)
from coding_agent.workflow.schema import Workflow
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.worker.startup import Ready

WorkflowRunner = Callable[..., WorkflowResult]
"""Runs the Workflow graph for one Attempt (production or a test fake)."""


def _live_gate(
    tracker: StepTrackerPort,
    pin: PinnedInput,
    *,
    selection_label: str,
    base_branch: str,
) -> GateCheck:
    """The live SIGTERM-finalisation Gate over the Claim pin (§12, §7.3).

    Mirrors the restart Gate: re-reads the input and the base tip and
    answers Drift or withdrawal through :func:`check_gate`, so the shutdown
    finalisation ends ``withdrawn`` where the input was withdrawn meanwhile
    instead of ``interrupted`` / ``shutdown``.
    """

    def gate() -> GateResult:
        current = tracker.fetch_input(pin.input_number, pin.input_type)
        try:
            base_tip_now = tracker.fetch_base_tip(base_branch)
        except Exception:
            base_tip_now = None
        return check_gate(
            current,
            pin,
            selection_label=selection_label,
            base_tip_now=base_tip_now,
        )

    return gate


def build_agent_step_deps(
    *,
    attempt: StepAttemptFacts,
    model: Any,
    workspace_path: Path,
    tree_id: str,
    activation_registry: ActivationRegistry,
    prompt_texts: Mapping[str, str],
    ledger: RunLedger,
    budgets: AttemptBudgets,
    prices: TokenPrices,
    output_cap: int,
    context_window: ContextWindowConfig | None,
    pin: PinnedModel,
    redactions: Redactable,
    attempt_branch: str | None,
    profile: ProjectProfile,
    command_runner: CommandRunner,
    evidence_dir: Path,
    transcript: TranscriptWriter,
    archive_dir: Path | None,
    on_activity: Callable[[], None] | None = None,
    handoff_note: str | None = None,
) -> AgentStepDeps:
    """Build every agent Step Execution's deps exactly as the Attempt run does.

    The one production wiring for :class:`AgentStepDeps`: the Attempt's one
    Pinned Model under the live Transcript, the redaction registry, and the
    step's Pinned Model context policy (contract §9.9).
    """
    deps = AgentStepDeps(
        attempt=attempt,
        model=TranscriptModel(model, transcript),
        workspace=workspace_path,
        tree_id=tree_id,
        activation_registry=activation_registry,
        prompt_texts=dict(prompt_texts),
        ledger=ledger,
        budgets=budgets,
        prices=prices,
        output_cap=output_cap,
        context_window=context_window,
        pin=pin,
        is_gateway=pin.is_gateway,
        redactions=redactions,
        attempt_branch=attempt_branch,
        profile=profile,
        runner=command_runner,
        evidence_dir=evidence_dir,
        on_activity=on_activity,
    )
    deps.archive_dir = archive_dir
    deps.handoff_note = handoff_note
    return deps


def pinned_profile_text(ledger: RunLedger, attempt_id: str) -> str | None:
    """The Attempt's pinned Validation Contract text, where one was pinned (ADR 0030).

    Reads the ``project_profile`` section from the Attempt's recorded
    Effective Configuration, then the digest-addressed blob from the Attempt
    Archive, decoded as strict UTF-8. ``None`` where the record carries no
    ``project_profile`` key — the profile comes from the Target Repository
    and the runner re-reads it at the start commit. ``ValueError`` where the
    section is malformed or the blob is missing or undecodable: recovery
    never guesses a contract.
    """
    from coding_agent.operator.archive import content_blob_path

    record = ledger.get_attempt(attempt_id)
    section = record.effective_configuration.get("project_profile")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError(
            f"cannot run Attempt {attempt_id!r}: "
            "its Effective Configuration carries a malformed project_profile section"
        )
    digest = section.get("sha256")
    if not isinstance(digest, str) or not digest:
        raise ValueError(
            f"cannot run Attempt {attempt_id!r}: "
            "its Effective Configuration carries a project_profile without a digest"
        )
    archive_dir = ledger.archive_dir_for(attempt_id)
    if archive_dir is None:
        raise ValueError(
            f"cannot run Attempt {attempt_id!r}: "
            "its Attempt Archive cannot be resolved"
        )
    try:
        raw = content_blob_path(archive_dir, digest).read_bytes()
    except OSError as exc:
        raise ValueError(
            f"cannot run Attempt {attempt_id!r}: "
            f"its pinned Project Profile blob {digest!r} cannot be read: {exc}"
        ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"cannot run Attempt {attempt_id!r}: "
            "its pinned Project Profile blob is not valid UTF-8"
        ) from exc


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
        branch = AttemptId(
            ready.workflow.id,
            claimed.input_number,
            claimed.n,
        ).branch(pin_facts.title)
        # A pickup Claim opens a Continuation at the recorded work commit on
        # the new Attempt branch (§8): the remote still serves the exact work
        # commit at its named ref with the recorded Base Revision in history.
        # Any failed verification raises GitFailure, and the Attempt ends
        # ``internal-failure`` through the poll loop's infrastructure path —
        # never model work on a moved or unavailable revision.
        #
        # A pull-request Attempt instead opens its pinned Input Head on the
        # pull request's head branch (§7.2, §9.10): the remote serves the
        # exact head SHA, which is fetched and checked out before any model
        # work. A missing head fails the same way — GitFailure before any
        # model call — never model work on the base-branch tip.
        continuation_note: str | None = None
        if claimed.pickup_offer is not None:
            checkout_continuation(
                workspace_path=workspace.path,
                canonical_remote=canonical_remote,
                offer=claimed.pickup_offer,
                attempt_branch=branch,
                helper=helper,
                redact=registry,
            )
            continuation_note = claimed.pickup_offer.note
            working_branch = branch
        elif pin_facts.input_type == "pull-request":
            if not pin_facts.head_branch:
                raise GitFailure(
                    "pull-request: the pinned head branch is missing"
                )
            if not pin_facts.input_head:
                raise GitFailure(
                    "pull-request: the pinned Input Head is missing"
                )
            checkout_pull_request(
                workspace_path=workspace.path,
                canonical_remote=canonical_remote,
                input_head=pin_facts.input_head,
                head_branch=pin_facts.head_branch,
                helper=helper,
                redact=registry,
            )
            working_branch = pin_facts.head_branch
        else:
            working_branch = branch
        start_commit = head_revision(workspace.path)
        evidence_dir = (
            ready.state_dir
            / "evidence"
            / ready.owner
            / ready.repo
            / str(claimed.input_number)
            / str(claimed.n)
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        # The Attempt id is known at Claim: resolve the Attempt Archive
        # once here, never per step inside the graph. The live Transcript
        # is contract-shaped (§13.3) and lives in the archive; where the
        # archive cannot resolve the Attempt still runs and the Transcript
        # falls back to the MVP evidence layout.
        archive_dir = ready.ledger.archive_dir_for(claimed.attempt_id)
        transcript = TranscriptWriter(
            (archive_dir / TRANSCRIPT_FILE_NAME)
            if archive_dir is not None
            else (evidence_dir / TRANSCRIPT_FILE_NAME),
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
            current_revision=(
                claimed.pickup_offer.work_commit
                if claimed.pickup_offer is not None
                else (
                    pin_facts.input_head
                    if pin_facts.input_type == "pull-request"
                    and pin_facts.input_head is not None
                    else pin_facts.base_revision
                )
            ),
            base_branch=pin_facts.base_branch
            or ready.config.target.base_branch,
            head_branch=pin_facts.head_branch,
            input_head=pin_facts.input_head,
            base_tip=pin_facts.base_tip
            if pin_facts.input_type == "pull-request"
            else None,
            comments=tuple(claimed.pinned_comments),
        )
        attempt_budgets = build_budgets(
            ready.workflow,
            prices=ready.price,
            output_cap=ready.output_cap,
            attempt_id=claimed.attempt_id,
            is_gateway=ready.pin.is_gateway,
        )
        author_name, author_email = noreply_author(ready.identity)
        local_runner = LocalOperationRunner(
            workspace=workspace.path,
            start_commit=start_commit,
            matrix=ready.matrix,
            service_env={},
            prober=SocketServiceProber(),
            runner=ready.command_runner,
            evidence_dir=evidence_dir,
            author_name=author_name,
            author_email=author_email,
            attempt_branch=working_branch,
            input_type=str(pin_facts.input_type),
            head_branch=pin_facts.head_branch,
            expected_head=pin_facts.input_head,
            base_tip=pin_facts.base_tip
            if pin_facts.input_type == "pull-request"
            else None,
            input_number=claimed.input_number,
            git=LiveCommitPushGitOps(
                canonical_remote, helper=helper, redact=registry
            ),
            remote_url=canonical_remote,
            helper=helper,
            redact=registry,
            profile_text_override=pinned_profile_text(
                ready.ledger, claimed.attempt_id
            ),
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
        deps = build_agent_step_deps(
            attempt=facts,
            model=ready.model,
            workspace_path=workspace.path,
            tree_id=tree_identity(workspace.path),
            activation_registry=ready.activation_registry,
            prompt_texts=ready.prompt_texts,
            ledger=ready.ledger,
            budgets=attempt_budgets,
            prices=ready.price,
            output_cap=ready.output_cap,
            context_window=ready.context_window,
            pin=ready.pin,
            redactions=registry,
            attempt_branch=working_branch,
            profile=profile,
            command_runner=ready.command_runner,
            evidence_dir=evidence_dir,
            transcript=transcript,
            archive_dir=archive_dir,
            on_activity=(live.touch if live is not None else None),
            handoff_note=continuation_note,
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
            workflow=ready.workflow,
            attempt_branch=working_branch,
            canonical_remote=canonical_remote,
            helper=helper,
            redact=registry,
            gate=_live_gate(
                ready.tracker,
                claimed.pin,
                selection_label=ready.workflow.input.label,
                base_branch=claimed.pin.base_branch
                or ready.config.target.base_branch,
            ),
            shutdown_grace_seconds=ready.config.worker.shutdown_grace_seconds,
        )
        from coding_agent.operator.runlog import print_line

        try:
            stored = ready.ledger.get_outcome(result.attempt_id)
        except Exception:
            stored = None
        outcome = stored.outcome if stored is not None else result.outcome
        reason = stored.reason if stored is not None else result.reason
        print_line(
            f"run: {outcome} ({reason}); "
            f"attempt={result.attempt_id}{_spend_suffix(ready.ledger, result.attempt_id)}"
        )
        print_line(f"run: transcript {transcript.path}")
        return result


def _spend_suffix(ledger: RunLedger, attempt_id: str) -> str:
    """The Attempt spend suffix for the end-of-Attempt line (best-effort).

    Reuses the spend projection the status snapshot already uses
    (:func:`operator.step_projection.spend_from_executions`): summed tokens,
    cost, tool calls and wall-clock seconds since the Attempt opened. An
    unmetered Attempt prints zeros and ``elapsed 0.0s`` rather than failing
    the line.
    """
    from datetime import datetime, timezone

    from coding_agent.operator.step_projection import spend_from_executions

    try:
        executions = ledger.list_step_executions(attempt_id)
        spend = spend_from_executions(executions)
        tokens = spend.get("effective_tokens", 0)
        cost = spend.get("cost_usd", 0.0)
        tool_calls = spend.get("tool_calls", 0)
        wall_clock = spend.get("wall_clock_seconds")
        try:
            opened_at = ledger.attempt_opened_at(attempt_id)
            elapsed = (datetime.now(timezone.utc) - opened_at).total_seconds()
            elapsed = max(0.0, elapsed)
        except Exception:
            elapsed = float(wall_clock) if isinstance(wall_clock, (int, float)) else 0.0
        try:
            cost_value = float(cost)
        except (TypeError, ValueError):
            cost_value = 0.0
        try:
            tokens_value = int(tokens)
        except (TypeError, ValueError):
            tokens_value = 0
        try:
            calls_value = int(tool_calls)
        except (TypeError, ValueError):
            calls_value = 0
        return (
            f"; tokens={tokens_value} cost_usd={cost_value:.4f} "
            f"tool_calls={calls_value} elapsed={elapsed:.1f}s"
        )
    except Exception:
        return ""


def _resolve_live_handoff_note(
    *,
    ledger: RunLedger,
    attempt_id: str,
    annotation_kind: str | None,
    annotation_note: str | None,
    context: Sequence[str] = (),
) -> str | None:
    """Resolve the live round's handoff note text, bounded (§8).

    An ``ai-assisted`` round resolves its declared note Step Artifact; a
    ``hard`` round composes the model-free account from the ledger's
    available Execution Summaries. ``None`` where no note could be built —
    an empty note is a missing note, never an empty offer.
    """
    if annotation_kind == "ai-assisted":
        if not annotation_note:
            return None
        try:
            text, pointer = resolve_document(ledger, attempt_id, annotation_note)
        except DocumentResolutionError:
            return None
        bounded, _ = truncate_note(text, pointer=pointer)
        return bounded or None
    if annotation_kind == "hard":
        summaries = [
            SummaryInput(
                step_id=record.step_id,
                seq=record.seq,
                facts=record.facts or {},
            )
            for record in ledger.list_step_executions(attempt_id)
            if record.facts
        ]
        return hard_handoff_note(summaries, context=context)
    return None


def _live_push_evidence(
    *,
    ledger: RunLedger,
    attempt_id: str,
    ref: str,
) -> tuple[bool, str | None]:
    """The publishing push evidence: landed flag and revision (§8, §14.1).

    Scans the ledger's step effects for the latest ``commit-and-push``
    receipt naming this Attempt branch. ``no-changes`` rounds record no
    receipt, so the remote read-back below still gets its say.
    """
    landed_revision: str | None = None
    for effect in ledger.list_step_effects(attempt_id):
        if effect.status != "receipt":
            continue
        target = effect.expected_target or {}
        if target.get("operation") != "commit-and-push":
            continue
        if target.get("branch") != ref:
            continue
        if not effect.remote_id:
            continue
        revision = target.get("revision")
        landed_revision = (
            revision if isinstance(revision, str) and revision else effect.remote_id
        )
    return landed_revision is not None, landed_revision


def _read_live_remote_evidence(
    *,
    workspace_path: Path,
    canonical_remote: str,
    ref: str,
    base_revision: str | None,
    helper: GitCredentialHelper | None,
    redact: Redactable,
) -> tuple[str | None, str | None, bool]:
    """Read the workspace HEAD and verify it against the remote (§8).

    Returns the work commit, the sha the remote serves at the Attempt
    branch ref, and whether the Base Revision is an ancestor of the work
    commit. Any transport or git failure degrades to ``None``/``False`` —
    the decision below diagnoses the round instead of failing finalisation.
    """
    try:
        work_commit = head_revision(workspace_path)
    except Exception:
        return None, None, False
    try:
        remote_sha = remote_ref_sha(
            canonical_remote, ref, redact=redact, helper=helper
        )
    except Exception:
        remote_sha = None
    ancestor = False
    if base_revision and work_commit:
        try:
            ancestor = is_ancestor(
                workspace_path, base_revision, work_commit, redact=redact
            )
        except GitFailure:
            ancestor = False
        except Exception:
            ancestor = False
    return work_commit, remote_sha, ancestor


def verify_offer_remote(
    *,
    mirror_dir: Path,
    canonical_remote: str,
    offer: RoundHandoffOffer,
    helper: GitCredentialHelper | None = None,
    redact: Redactable = None,
) -> bool:
    """Whether the remote still serves the offer's revisions (§8).

    The intake availability check: the named ref serves exactly the recorded
    work commit and the recorded Base Revision is in its history (read in the
    mirror, which the Worker keeps updated). Any transport failure or mismatch
    reads ``False`` — the sweep leaves the signal untouched and claims
    nothing, so a previously advertised revision that is no longer available
    is refused without a pickup offer.
    """
    try:
        remote_sha = remote_ref_sha(
            canonical_remote, offer.ref, redact=redact, helper=helper
        )
    except Exception:
        return False
    if remote_sha != offer.work_commit:
        return False
    try:
        return bool(
            is_ancestor(
                mirror_dir, offer.base_revision, offer.work_commit, redact=redact
            )
        )
    except Exception:
        return False


def checkout_continuation(
    *,
    workspace_path: Path,
    canonical_remote: str,
    offer: RoundHandoffOffer,
    attempt_branch: str,
    helper: GitCredentialHelper | None = None,
    redact: Redactable = None,
) -> str:
    """Open the Continuation workspace at the recorded work commit (§8).

    Verifies the remote still serves the offer's work commit at its named
    ref, fetches the exact work commit and Base Revision, proves the Base
    Revision is an ancestor of the work commit, then opens the new Attempt
    branch at the work commit with a clean tree. The recorded Base Revision
    is retained even when the configured base branch has moved: ancestry is
    proven against the recording, never the current tip. Returns the work
    commit. Any failed verification raises :class:`GitFailure`: the Attempt
    ends ``internal-failure`` through the poll loop's infrastructure path,
    never model work on a moved or unavailable revision.
    """
    try:
        remote_sha = remote_ref_sha(
            canonical_remote, offer.ref, redact=redact, helper=helper
        )
    except GitFailure as exc:
        raise GitFailure(
            f"continuation: cannot verify ref {offer.ref!r} "
            f"for {offer.attempt}: {exc}"
        ) from exc
    if remote_sha != offer.work_commit:
        served = remote_sha if remote_sha is not None else "nothing"
        raise GitFailure(
            f"continuation: ref {offer.ref!r} serves {served}, "
            f"not work commit {offer.work_commit} for {offer.attempt}"
        )
    fetch_revision(
        workspace_path,
        canonical_remote,
        offer.work_commit,
        redact=redact,
        helper=helper,
    )
    fetch_revision(
        workspace_path,
        canonical_remote,
        offer.base_revision,
        redact=redact,
        helper=helper,
    )
    try:
        ancestor = is_ancestor(
            workspace_path, offer.base_revision, offer.work_commit, redact=redact
        )
    except GitFailure as exc:
        raise GitFailure(
            f"continuation: cannot verify Base Revision "
            f"{offer.base_revision} for {offer.attempt}: {exc}"
        ) from exc
    if not ancestor:
        raise GitFailure(
            f"continuation: Base Revision {offer.base_revision} is not an "
            f"ancestor of work commit {offer.work_commit} for {offer.attempt}"
        )
    checkout_branch_at(workspace_path, attempt_branch, offer.work_commit)
    return offer.work_commit


def checkout_pull_request(
    *,
    workspace_path: Path,
    canonical_remote: str,
    input_head: str,
    head_branch: str,
    helper: GitCredentialHelper | None = None,
    redact: Redactable = None,
) -> str:
    """Open a pull-request Attempt at its pinned Input Head (§7.2, §9.10).

    Fetches the exact pinned head SHA the remote serves, then opens a local
    branch named exactly after the pull request's head branch at that commit
    with a clean tree. The Base Revision stays as pinned at Claim (the merge
    base of the head and the base tip) and is never re-resolved here. Returns
    the Input Head. Any failed fetch or checkout raises :class:`GitFailure`:
    the Attempt ends ``internal-failure`` through the poll loop's
    infrastructure path before any model call — never model work on the
    base-branch tip.
    """
    if not head_branch:
        raise GitFailure("pull-request: the pinned head branch is missing")
    if not input_head:
        raise GitFailure("pull-request: the pinned Input Head is missing")
    try:
        fetch_revision(
            workspace_path,
            canonical_remote,
            input_head,
            redact=redact,
            helper=helper,
        )
    except GitFailure as exc:
        raise GitFailure(
            f"pull-request: cannot fetch Input Head {input_head} "
            f"for head branch {head_branch!r}: {exc}"
        ) from exc
    try:
        checkout_branch_at(workspace_path, head_branch, input_head)
    except GitFailure as exc:
        raise GitFailure(
            f"pull-request: cannot check out Input Head {input_head} "
            f"on head branch {head_branch!r}: {exc}"
        ) from exc
    return head_revision(workspace_path)


def _decide_live_handoff(
    *,
    ledger: RunLedger,
    workflow: Workflow,
    result: WorkflowResult,
    number: int,
    workspace_path: Path,
    attempt_branch: str,
    canonical_remote: str,
    helper: GitCredentialHelper | None = None,
    redact: Redactable = None,
) -> RoundHandoffOffer | HandoffDiagnostic | None:
    """Decide the live round: offer, diagnostic, or no handoff at all (§8).

    ``None`` off the publishing path — Round Handoff disabled, a
    non-``unfinished`` outcome, or a taken end carrying no publishing
    annotation — so the Attempt keeps its ordinary terminal path. Otherwise
    the note, the push receipt and the remote read-back become one
    preparation for :func:`decide_handoff`, which diagnoses whatever the
    round failed to publish.
    """
    enablement = workflow.round_handoff
    if enablement is None:
        return None
    if not (result.ended and result.outcome == "unfinished"):
        return None
    step = workflow.steps.get(result.ending_step)
    route = step.routes.get(result.step_result) if step is not None else None
    annotation = (
        route.round_handoff
        if route is not None and route.end == "unfinished"
        else None
    )
    if annotation is None or annotation.kind not in ("ai-assisted", "hard"):
        return None
    note = _resolve_live_handoff_note(
        ledger=ledger,
        attempt_id=result.attempt_id,
        annotation_kind=annotation.kind,
        annotation_note=annotation.note,
        context=(result.reason,) if result.reason else (),
    )
    pin = None
    try:
        pin = ledger.get_attempt_pin(result.attempt_id)
    except Exception:
        pin = None
    base_revision = pin.base_revision if pin is not None else None
    push_landed, landed_revision = _live_push_evidence(
        ledger=ledger, attempt_id=result.attempt_id, ref=attempt_branch
    )
    work_commit, remote_sha, ancestor = _read_live_remote_evidence(
        workspace_path=workspace_path,
        canonical_remote=canonical_remote,
        ref=attempt_branch,
        base_revision=base_revision,
        helper=helper,
        redact=redact,
    )
    if (
        remote_sha is not None
        and work_commit is not None
        and remote_sha == work_commit
    ):
        push_landed = True
        landed_revision = landed_revision or remote_sha
    if result.step_result == "no-changes":
        # A clean tree publishes without a push: the remote verification
        # below is the publication, so no push receipt is required. A ref
        # the remote cannot serve still diagnoses as unavailable-revision
        # rather than failed-push.
        push_landed = True
    remote_verified = (
        work_commit is not None
        and base_revision is not None
        and remote_sha == work_commit
        and ancestor
    )
    preparation = build_live_preparation(
        workflow_id=workflow.id,
        issue=number,
        attempt=result.attempt_id,
        outcome=result.outcome,
        ending_step=result.ending_step,
        step_result=result.step_result,
        pickup_label=enablement.pickup_label,
        limit=enablement.max_automatic_pickups,
        annotation_kind=annotation.kind,
        note_text=note,
        work_commit=work_commit,
        ref=attempt_branch,
        base_revision=base_revision,
        push_landed=push_landed,
        remote_verified=remote_verified,
        landed_revision=landed_revision,
        selection_actor=pin.selection_actor if pin is not None else None,
        selection_actor_id=pin.selection_actor_id if pin is not None else None,
        selection_actor_type=pin.selection_actor_type if pin is not None else None,
        selection_actor_at=pin.selection_actor_at if pin is not None else None,
        identity_mode=pin.identity_mode if pin is not None else None,
        used=0,
    )
    if preparation is None:
        return None
    return decide_handoff(preparation)


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
    workflow: Workflow,
    attempt_branch: str,
    canonical_remote: str,
    helper: GitCredentialHelper | None = None,
    redact: Redactable = None,
    gate: GateCheck | None = None,
    shutdown_grace_seconds: float | None = None,
) -> None:
    """Publish the live Attempt's terminal writes after the Workflow run.

    The graph never records a Terminal Outcome: this builds the outcome
    decision from the run result — a reached Workflow End, a Gate stop, a
    pin violation, an observed SIGTERM, or an internal failure — decides
    the Round Handoff round from the live evidence, and ends the Attempt
    through the finalisation entry point, which records once before
    publishing (§15.2, §15.3, §8). A SIGTERM observed at a step boundary
    (``interrupted`` / ``shutdown``, never ended) finalises through the
    shutdown path instead: the in-flight write settles under the shutdown
    grace bound and the Attempt ends ``interrupted`` / ``shutdown``, or
    ``withdrawn`` where the input was withdrawn meanwhile (§12).
    """
    if result.outcome == "interrupted" and result.reason == SHUTDOWN_REASON:
        try:
            uncommitted = count_uncommitted_files(workspace_path)
        except Exception:
            uncommitted = 0
        deadline: float | None = None
        if shutdown_grace_seconds is not None:
            try:
                deadline = shutdown_grace_deadline(shutdown_grace_seconds)
            except Exception:
                deadline = None
        finalise_shutdown(
            tracker=tracker,
            ledger=ledger,
            attempt_id=result.attempt_id,
            number=number,
            input_type=input_type,
            selection_label=selection_label,
            end_labels_for=end_labels_for,
            gate=gate,
            uncommitted_count=uncommitted,
            ending_step=result.ending_step,
            step_result=result.step_result,
            workflow_end=result.workflow_end,
            deadline=deadline,
        )
        return
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
    handoff: RoundHandoffOffer | HandoffDiagnostic | None = None
    try:
        handoff = _decide_live_handoff(
            ledger=ledger,
            workflow=workflow,
            result=result,
            number=number,
            workspace_path=workspace_path,
            attempt_branch=attempt_branch,
            canonical_remote=canonical_remote,
            helper=helper,
            redact=redact,
        )
    except Exception:
        handoff = None
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
        handoff=handoff,
    )


__all__ = [
    "WorkflowRunner",
    "checkout_continuation",
    "checkout_pull_request",
    "pinned_profile_text",
    "run_attempt",
    "verify_offer_remote",
]
