"""The fixed eleven-node Attempt graph, from ``claim`` to ``terminal_writes``.

Contract: ``docs/contract/mvp-runtime-contract.md`` §1 (one fixed graph
whatever the Workflow says; no merge node; no framework checkpointer — the
Run Ledger is the only durable state), §6 (the nodes in order), §7
(readiness resolves the toolchain matrix and the declared services before
any model call), §8 (opening a step: the Pinned Prefix, then the forced
activation call; the Attempt Header), §9 (completion is declared and
substantiated, never inferred: the runtime-owned targeted-test tool is the
sole writer of the per-Attempt evidence dir and records Criterion Evidence;
``report_completion`` is the only non-exceptional exit with a per-Attempt
schema; an unbacked claim is returned naming every failing entry, never a
progress event; after two returns the third call is force-accepted as
``implemented but unverified`` with a diff or ``completion not
established`` / ``claims-unverified`` without one; an explicit
``uncovered`` is accepted as ``criteria-uncovered``; the stage ends in
exactly one Implementation Outcome decided from the claim and Criterion
Evidence alone; only ``verified completion`` may use PASS wording), §14
("Recording before publishing": ``terminal_writes`` records the Terminal
Outcome before it performs anything), §17 (the ledger tables), §10 (the
review snapshot commits the current tree and pushes the Attempt's branch
before either reviewer starts), §13 (the write sequence keyed by
``(attempt_id, write_kind)``) and §18 (one branch per Attempt).

MVP row ids: C-GRF-1, C-SKL-1, C-SKL-2, G-HDR-1, L1-6, L3-SEL-11,
L3-CLR-19, L3-CLR-20, L3-IMP-1, L3-IMP-4, L3-IMP-5, L3-IMP-9, L3-IMP-10,
L3-IMP-11, L3-IMP-12, L3-IMP-13, L3-IMP-15, L3-IMP-16, L3-IMP-17, L3-IMP-18,
L3-IMP-20, L3-IMP-21, L3-IMP-23, L3-IMP-24, L3-IMP-25, L3-IMP-26, L3-IMP-30,
L3-IMP-31, C-CTX-1, L3-ERR-2, L3-ERR-3, P-WRK-1, P-WRK-2,
C-LIM-1, P-LIM-1, L3-IMP-7,
P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4,
C-RDY-1, C-RDY-2, C-RDY-3, C-RDY-4, C-WSP-1, L2-1, L2-2, L2-3, L2-12, L2-13, L2-14,
L2-17, L2-18,
P-CTX-1, C-IMP-1, L3-IMP-28, L3-IMP-29, W-STP-1, W-STP-2, W-STP-4, W-STP-5,
G-STP-1, C-STP-1, C-ERR-1, C-ERR-2, L3-DEL-19, L3-DEL-20, L3-DEL-21,
L3-DEL-14, L3-ERR-9, L3-ERR-10,
P-DEL-1, P-DEL-2, P-DEL-4, P-DEL-5, P-DEL-6, P-DEL-7, P-DEL-8, P-DEL-9,
P-DEL-10,
C-PUB-1, C-PUB-2, C-OUT-1, G-TDD-1, L3-DEL-5, L3-REV-10, L2-17, L2-18,
C-REV-1, L3-REV-1, L3-REV-2, G-REV-1,
G-REV-2, W-STP-6, W-PIN-2, P-CMD-2, L1-3,
P-CAN-1, P-CAN-2, P-CAN-3, W-STP-3, G-REV-3, REC-SIG-1, REC-SIG-2.

Every node whose behaviour belongs to a later ticket or slice
(``gate``, ``publication_gate``, ``delivery_intent``, ``publish``) is a
pass-through until that slice fills it; the graph's shape never changes.
``delivery_snapshot`` commits and pushes the candidate tree for every
Implementation Outcome and ``final_checks`` runs the pinned Validation
Contract once for ``verified completion``. ``readiness``
rebuilds the workspace, pins the profile at the Base Revision as the
Validation Contract, and resolves every Readiness Fact from the profile and
the Target Issue alone. ``baseline`` runs that pinned contract's
``bootstrap``, then ``test_all`` and every check, on the tree at the Base
Revision before any model call; a bootstrap failure ends ``failed`` /
``infrastructure-failure`` (class ``setup``) and a red baseline ends
``failed`` / ``red-baseline``, each routed straight to ``terminal_writes``
with no model call. A refusal in either node sets the Terminal Outcome
here and every later node passes it through.
Runtime context (meters, the tracker port, Criterion Evidence) reaches nodes
through ``RunnableConfig``, never through graph state. An unclassified
exception anywhere in the graph ends the Attempt ``failed`` — permanent, not
transient — and the outcome is still recorded.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from coding_agent.github.errors import (
    CredentialFailure,
    GitHubFailure,
    PermissionRefusal,
    RateLimitExhausted,
)
from coding_agent.github.issues import IssueFetchFailed
from coding_agent.tracker.github import GitHubTrackerError
from coding_agent.identity.commit_identity import attempt_branch_name
from coding_agent.identity.startup import Identity
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime.fingerprint import compute_fingerprint
from langchain_core.exceptions import ModelError

from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber, SocketServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix
from coding_agent.runtime.baseline import (
    baseline_evidence_payload,
    baseline_outcome,
    run_baseline,
)
from coding_agent.runtime.ceilings import (
    LoopCeilings,
    UsageTotals,
    ceiling_crossed,
    messages_usage,
)
from coding_agent.runtime.delivery import (
    DeliverySnapshotRefused,
    publish_delivery_snapshot,
)
from coding_agent.runtime.gates import CANCELLED_REASON, GateStop, check_cancellation
from coding_agent.runtime.interrupted import finalize_interrupted_attempt
from coding_agent.runtime.shutdown import INTERRUPTED_REASON, is_shutdown_requested
from coding_agent.runtime.final_checks import (
    FINAL_CHECKS_EVIDENCE_SUBDIR,
    FINAL_CHECKS_KIND,
    FinalChecksReport,
    candidate_changed_profile,
    dirty_working_tree_files,
    final_checks_evidence_payload,
    final_checks_green,
    run_final_checks,
)
from coding_agent.runtime.git import (
    GitFailure,
    Workspace,
    head_revision,
    read_file_at_revision,
    remote_ref_sha,
    tree_differs_from_base,
)
from coding_agent.runtime.publication import (
    ADD_READY_FOR_HUMAN_WRITE_KIND,
    DELIVERY_COMMENT_KIND,
    DELIVERY_INTENT_KIND,
    DIRTY_TREE_REASON,
    DRIFT_REASON,
    FAILING_VALIDATION_REASON,
    PUBLICATION_GATE_KIND,
    PUBLISH_WRITE_KIND,
    RELEASE_ASSIGNMENT_WRITE_KIND,
    REMOVE_SELECTION_LABEL_WRITE_KIND,
    REVIEW_MISSING_REASON,
    TERMINAL_COMMENT_WRITE_KIND,
    VERIFIED_COMPLETION_REASON,
    WITHDRAWAL_REASON,
    attempt_marker,
    baseline_detail_for_comment,
    compose_terminal_snapshot,
    evaluate_publication_gate,
    render_pull_request_body,
    render_snapshot_block,
    render_terminal_comment,
    terminal_next_action,
    terminal_writes_for_outcome,
)
from coding_agent.runtime.review_snapshot import (
    REVIEW_STATUS_KIND,
    WORKFLOWS_FAILURE_REASON,
    review_disclosure,
)
from coding_agent.runtime.readiness import PROFILE_RELPATH
from coding_agent.validate.runner import CommandRunner, SubprocessCommandRunner
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.completion import (
    ClaimRecord,
    CriterionEvidence,
    decide_implementation_outcome,
    normalize_claim,
)
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.completion import (
    MAX_STEP_JUSTIFICATION_CHARS,
    REPORT_STEP_CANNOT_CONTINUE,
    REPORT_STEP_DONE,
    StepReportRecord,
)
from coding_agent.runtime.context_handoff import (
    CONTINUATION_SNAPSHOT_VERSION,
    HANDOFF_FAILURE_KIND,
    MAX_HANDOFFS_PER_STEP,
    STEP_BOUNDARY_KIND,
    ContinuationSnapshot,
    ContextWindowConfig,
    HandoffFailure,
    build_handoff_prompt,
    continuation_message,
    estimate_context_tokens,
    execute_handoff_turn,
    handoff_event_payload,
    persist_handoff_failure,
    persist_snapshot,
    step_boundary_event_payload,
)
from coding_agent.runtime.step_progress import StepProgress
from coding_agent.runtime.graph.step import (
    DEFAULT_STEPS,
    WorkflowStep,
    check_opening_fits_context,
    has_report_completion,
    has_step_report,
    report_completion_args,
    run_work_node,
    step_report_args,
)
from coding_agent.runtime.silence import (
    MAX_SILENCE_CONTINUATIONS,
    SILENCE_EXHAUSTED_REASON,
    compose_silence_continuation,
)
from coding_agent.runtime.result_capping import (
    DEFAULT_RESULT_CAP_LIMIT,
    ArtifactStore,
    FilesystemArtifactStore,
    InMemoryArtifactStore,
)
from coding_agent.runtime.graph.tools import build_implement_step_tools
from coding_agent.runtime.pinned_prefix import (
    build_step_opening_messages,
    compose_mvp_attempt_header,
    enumerate_acceptance_criteria,
)
from coding_agent.runtime.readiness import (
    ReadinessRefusal,
    default_toolchain_matrix,
    resolve_readiness,
)
from coding_agent.runtime.seam import derive_seam_set
from coding_agent.tracker.port import TrackerPort


def _default_service_prober() -> ServiceProber:
    return SocketServiceProber()


ATTEMPT_NODE_ORDER = (
    "claim",
    "readiness",
    "baseline",
    "run_step",
    "gate",
    "delivery_snapshot",
    "final_checks",
    "publication_gate",
    "delivery_intent",
    "publish",
    "terminal_writes",
)
"""The fixed node order of contract §6. Asserted structurally, not by reading."""

DELIVERED = "delivered"
FAILED = "failed"
ABANDONED = "abandoned"
"""The Terminal Outcome for a withdrawal or a pre-Intent cancellation (§14)."""
FAILED_LIMIT = "failed-limit"
"""The Terminal Outcome for a crossed ceiling before the Delivery Intent (§11).

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Hard ceilings":
a crossed ceiling stops the loop inside the node with usage flushed, then a
deterministic finalization commits and pushes with no final checks, ending
``failed-limit`` naming the limit) and §14 (the Implementation Outcome →
Terminal Outcome table).

MVP row ids: C-LIM-1, P-LIM-1, L3-IMP-7.
"""


class AttemptState(TypedDict, total=False):
    """The Attempt graph's data plane only.

    Ids, the fetched issue and the outcome under construction travel here.
    Meters, Criterion Evidence, review records and the tracker port must never
    appear in this state — they reach nodes through ``AttemptContext``. The
    structural test asserts the annotation names carry none of them.
    """

    attempt_id: str
    issue_number: int
    attempt_number: int
    issue_title: str
    issue_body: str
    base_revision: str
    base_branch: str
    seam_set: list[str]
    acceptance_criteria: list[dict[str, str]]
    outcome: str
    reason: str
    claim: dict[str, list[str] | None]
    claim_force_accepted: bool


@dataclass(frozen=True)
class AttemptResult:
    """The externally observable end of an Attempt: outcome and reason."""

    attempt_id: str
    outcome: str
    reason: str


def claim(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§5: read the Target Issue back, then open the Attempt in the ledger.

    The Fetch comes first so the Fingerprint recorded beside the Attempt is
    the digest of the title and body this Attempt actually read (L1-6: the
    Pinned Model, endpoint included, arrives in the pinned Effective
    Configuration; the Fingerprint beside it). Where the fetch itself fails,
    ``run_attempt`` still opens the Attempt while recording the
    ``unclassified-error`` outcome, so recovery always finds a row.

    Under ``agent run`` the Worker's Claim has already opened the Attempt
    and confirmed it (§5: one ledger transaction with the Fingerprint, the
    Base Revision and the Agent Identity's id, then the assignment and its
    read-back). That row is the Attempt this graph runs: it is kept as the
    Claim recorded it, never opened a second time.
    """
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    issue = context.tracker.fetch_issue(state["issue_number"])
    if context.ledger.has_attempt(attempt_id):
        return {"issue_title": issue.title, "issue_body": issue.body}
    context.ledger.open_attempt(
        attempt_id,
        effective_configuration=context.effective_configuration,
        fingerprint=compute_fingerprint(issue.title, issue.body),
    )
    context.ledger.set_attempt_state(attempt_id, "claim-confirmed")
    return {"issue_title": issue.title, "issue_body": issue.body}


def readiness(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§7: rebuild the workspace, pin the profile, resolve every Readiness
    Fact — or refuse terminally with no model call.

    A refusal sets the Terminal Outcome here; every later node passes it
    through to ``terminal_writes``. The recorded reason names exactly which
    fact or key is missing or wrong.
    """
    if state.get("outcome"):
        return {}
    context = AttemptContext.from_config(config)
    matrix = (
        context.toolchain_matrix
        if context.toolchain_matrix is not None
        else default_toolchain_matrix()
    )
    service_env: Mapping[str, str] = (
        dict(context.service_env) if context.service_env is not None else dict(os.environ)
    )
    decision = resolve_readiness(
        context.workspace,
        state.get("issue_title") or "",
        state.get("issue_body") or "",
        matrix,
        service_env,
        context.service_prober,
    )
    if isinstance(decision, ReadinessRefusal):
        return {"outcome": FAILED, "reason": decision.reason}
    return {
        "base_revision": decision.base_revision,
        "base_branch": decision.base_branch,
        "seam_set": list(decision.seam_set),
        "acceptance_criteria": [
            {"id": criterion.id, "text": criterion.text}
            for criterion in decision.acceptance_criteria
        ],
    }


def baseline(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§7: run the pinned Validation Contract at the Base Revision.

    Runs ``bootstrap``, then ``test_all`` and every check, on the tree at
    the Base Revision that ``readiness`` rebuilt — before the step subgraph
    is ever invoked, so a refusal here reaches ``terminal_writes`` with no
    model call. A bootstrap failure ends ``failed`` /
    ``infrastructure-failure`` (class ``setup``); a red baseline (a non-zero
    exit or any reported test failure) ends ``failed`` / ``red-baseline``
    naming the failing commands and, where evidence provides them, the
    individual failure identifiers. A suite that executes zero tests and
    exits zero is not red. A check without an ``evidence`` block is judged
    by its exit code alone. Baseline Failure exclusion is deferred: a red
    baseline is refused, never worked around.

    A Readiness refusal recorded above passes straight through: no command
    runs and no model is called.
    """
    if state.get("outcome"):
        return {}
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    base_revision = state.get("base_revision")
    if not base_revision:
        return {
            "outcome": FAILED,
            "reason": (
                "missing-readiness-fact: base-revision "
                "(internal invariant violation: baseline entered with no Base Revision in state)"
            ),
        }
    profile_text = read_file_at_revision(context.workspace, base_revision, PROFILE_RELPATH)
    if profile_text is None:
        return {
            "outcome": FAILED,
            "reason": (
                "missing-readiness-fact: docs/agents/project-profile.yml: "
                "no Project Profile at the Base Revision"
            ),
        }
    parsed = parse_profile_yaml(profile_text)
    if not isinstance(parsed, ProjectProfile):
        return {"outcome": FAILED, "reason": f"invalid-profile: {parsed!r}"}
    profile = parsed
    runner: CommandRunner = (
        context.command_runner if context.command_runner is not None else SubprocessCommandRunner()
    )
    if context.evidence_dir is not None:
        evidence_dir = context.evidence_dir
        evidence_dir.mkdir(parents=True, exist_ok=True)
    else:
        evidence_dir = Path(tempfile.mkdtemp(prefix="coding-agent-baseline-"))
    working_directory = context.workspace / profile.working_directory
    report = run_baseline(profile, runner, working_directory, evidence_dir)
    context.ledger.record(
        attempt_id,
        "baseline",
        BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals()),
        detail="baseline Validation Evidence at the Base Revision (L2-1)",
        payload=baseline_evidence_payload(report),
    )
    refusal = baseline_outcome(report)
    if refusal is None:
        return {}
    outcome, reason = refusal
    return {"outcome": outcome, "reason": reason}


def _workspace_base_revision(workspace: Path) -> str:
    """The workspace's ``HEAD`` sha, or ``"unknown"`` where it cannot be read.

    Contract §8 (the Attempt Header carries the Base Revision). The M1 Attempt
    runs on a real checkout, so this succeeds; the fallback keeps a synthetic
    test workspace from failing the step for want of git rather than for want
    of behaviour.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        revision = result.stdout.strip()
        return revision if revision else "unknown"
    except Exception:
        return "unknown"


def _workspace_has_diff(workspace: Path, base_revision: str | None = None) -> bool:
    """Whether the working tree differs from its Base Revision commit.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (``verified
    completion`` needs a candidate diff; an empty diff without a complete
    per-criterion justification is ``completion not established``, never
    ``already satisfied``) and L3-IMP-10 (the tree identical to the Base
    Revision). The diff is measured against ``base_revision``, so a review
    snapshot that committed the tree (§10) does not hide it; see
    :func:`tree_differs_from_base`. An unreadable tree state never reports
    a clean tree.
    """
    known = base_revision if base_revision and base_revision != "unknown" else None
    return tree_differs_from_base(workspace, known)


def _workspace_on_current_branch(workspace_path: Path, *, base_revision: str) -> Workspace:
    """Wrap the Attempt working tree for a snapshot push (§10, §12, §13).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (when the model
    calls ``activate_code_review`` the runtime commits the current tree and
    pushes the Attempt's branch before any reviewer starts), §12 (the
    Delivery Snapshot commits the candidate tree and pushes the branch before
    the final checks) and §13 (one branch per Attempt, fast-forward only,
    never force).

    MVP row ids: L3-DEL-19, L3-DEL-20, P-DEL-4, C-REV-1, L3-REV-1, G-REV-1,
    C-PUB-1.

    The push path reads the live ``HEAD`` from git, so ``base_branch`` and
    ``base_revision`` travel only as context: the branch the tree is on (the
    base branch before the first snapshot, the Attempt branch after) and the
    Base Revision the Attempt Header carries. A tree whose branch is
    unreadable falls back to ``"main"``, mirroring
    ``_workspace_base_revision``.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=True,
        )
        base_branch = result.stdout.strip() or "main"
    except Exception:
        base_branch = "main"
    return Workspace(
        path=workspace_path, base_branch=base_branch, base_revision=base_revision
    )


def _handoff_changed_files(workspace: Path) -> list[str]:
    """Tracked and untracked working-tree paths for the handoff facts (§11).

    Best effort: an unreadable tree contributes no changed files rather than
    failing the handoff. ``Snapshot`` facts describe work; they never carry
    runtime policy state.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        files.append(stripped.split()[-1])
    return sorted(set(files))


def _handoff_commands(evidence: CriterionEvidence) -> list[dict[str, str]]:
    """Runtime-executed commands and results for the handoff facts (§11).

    The runtime owns Criterion Evidence (§9): only the targeted-test tool
    writes here, so this is the Attempt's own record of what ran, never the
    model's account of it.
    """
    return [
        {
            "command": f"test_targeted {record.test_id}",
            "result": "passed" if record.passed else "failed",
        }
        for record in evidence.history()
    ]


def _handoff_validation_evidence(evidence: CriterionEvidence) -> list[dict[str, object]]:
    """Available Validation Evidence for the handoff facts (§11, ADR 0013)."""
    return [
        {"test_id": record.test_id, "passed": record.passed}
        for record in evidence.history()
    ]


def _node_usage(messages: list[BaseMessage], price: TokenPrices | None) -> UsageTotals:
    """Provider-reported usage of one finished work node (contract §11).

    Sums the provider's own ``usage_metadata`` across the node's assistant
    turns (never the deterministic context estimate, which is the separate
    local control signal) plus every tool call the node issued, and costs
    each turn per token bucket from the Effective Configuration's prices
    (L1-3). ``None`` (the unit-test seam) meters no cost.
    """
    return messages_usage(messages, price)


def _attempt_elapsed_seconds(
    *,
    ledger: RunLedger,
    attempt_id: str,
    started_at: float,
) -> float:
    """Wall clock from the Attempt's ledger opening (§11, L3-IMP-7).

    Measured from the Run Ledger's Attempt start, never from process start,
    so review time counts too. Falls back to the node's monotonic start
    where no ledger Attempt exists (the unit-test seam).

    MVP row ids: L3-IMP-7.
    """
    try:
        return ledger.attempt_elapsed_seconds(attempt_id)
    except Exception:
        return max(0.0, time.monotonic() - started_at)


def _flush_ceiling_usage(
    *,
    progress: StepProgress,
    cumulative: UsageTotals,
    elapsed_seconds: float,
) -> None:
    """Flush cumulative usage before the hard-ceiling finalization (§11).

    A crossed ceiling stops the loop with the spend already durable: the
    readable ``usage`` record (per-bucket tokens and dollars) lands first,
    so the terminal comment reports the spend directly (C-LIM-1).

    MVP row ids: C-LIM-1, P-LIM-1.
    """
    progress.flush_usage(
        BudgetSnapshot(elapsed_seconds=elapsed_seconds, usage=cumulative)
    )


def _record_handoff_failure(
    *,
    ledger: RunLedger,
    store: ArtifactStore,
    attempt_id: str,
    sequence: int,
    error: str,
    excerpt: str,
    context_estimate_tokens: int,
    node_cap_tokens: int,
    cumulative: UsageTotals,
    elapsed_seconds: float,
    redactions: Mapping[str, str],
) -> dict[str, object]:
    """End the Attempt ``failed`` / ``handoff-failure`` (contract §11).

    Persists a redacted handoff-failure artifact, records the
    ``handoff_failure`` ledger event with the cumulative Attempt-wide usage,
    and starts no continuation (contract §11, L3-IMP-21). Where even the
    failure artifact cannot persist, the Attempt still ends
    ``failed`` / ``handoff-failure`` with the ledger event carrying no
    artifact reference.
    """
    try:
        failure_ref: str | None = persist_handoff_failure(
            store,
            attempt_id=attempt_id,
            sequence=sequence,
            error=error,
            excerpt=excerpt,
            redactions=redactions,
        )
    except HandoffFailure:
        failure_ref = None
    snapshot = BudgetSnapshot(elapsed_seconds=elapsed_seconds, usage=cumulative)
    payload: dict[str, object] = {
        "error": error,
        "snapshot_digest": "",
        "snapshot_version": 0,
        "context_estimate_tokens": context_estimate_tokens,
        "node_cap_tokens": node_cap_tokens,
        "provider_input_tokens": 0,
        "provider_output_tokens": 0,
        "cumulative_tokens": cumulative.tokens,
        "cumulative_effective_tokens": cumulative.effective_tokens,
        "cumulative_cost_usd": cumulative.cost_usd,
        "cumulative_tool_calls": cumulative.tool_calls,
    }
    try:
        ledger.record(
            attempt_id,
            HANDOFF_FAILURE_KIND,
            snapshot,
            detail=f"handoff {sequence} failed: {error}",
            artifact_ref=failure_ref,
            payload=payload,
        )
    except Exception:
        pass
    return {"outcome": FAILED, "reason": f"handoff-failure: {error}"}


def _compose_step_boundary_snapshot(
    *,
    attempt_id: str,
    base_revision: str,
    branch: str,
    commit: str,
    changed_files: list[str],
    commands: list[dict[str, str]],
    validation_evidence: list[dict[str, object]],
    justification: str,
    next_step_id: str,
    redactions: Mapping[str, str],
) -> ContinuationSnapshot:
    """Compose the step-boundary Continuation Snapshot (contract §8, W-STP-5).

    Built with the handoff mechanism ([ADR 0013](../../docs/adr/0013-context-window-handoff.md)):
    explicit runtime facts — the working tree the steps share, the
    runtime-owned commands and Validation Evidence — plus the previous step's
    ``report_step`` justification as a bounded field and a
    ``next_intended_action`` naming the next step. Redaction and the 32 KiB
    post-redaction cap are enforced by ``from_payload`` (C-CTX-1): an
    uncomposable boundary raises :class:`HandoffFailure`, which the caller
    maps to ``failed`` / ``handoff-failure``.

    MVP row ids: W-STP-5, C-CTX-1.
    """
    bounded = justification[:MAX_STEP_JUSTIFICATION_CHARS]
    payload: dict[str, object] = {
        "version": CONTINUATION_SNAPSHOT_VERSION,
        "attempt_id": attempt_id,
        "base_revision": base_revision,
        "branch": branch,
        "commit": commit,
        "changed_files": list(changed_files),
        "commands": [dict(command) for command in commands],
        "validation_evidence": list(validation_evidence),
        "unresolved_questions": [],
        "next_intended_action": f"continue with Workflow Step {next_step_id}",
        "step_justification": bounded,
    }
    return ContinuationSnapshot.from_payload(payload, redactions=redactions)


def _finalize_shutdown(
    state: AttemptState, context: AttemptContext
) -> dict[str, object]:
    """Take the claim-confirmed interrupted path for a live SIGTERM (§11, §17).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Before the
    Delivery Intent, the Attempt then takes the claim-confirmed path of the
    recovery table (§17) while the process is still alive, within
    ``shutdown_grace_seconds``: local commits are pushed fast-forward, the
    dirty tree is not committed, and the outcome is ``failed`` /
    ``interrupted``, or ``abandoned`` if the issue was cancelled meanwhile").

    MVP row ids: P-CAN-3, REC-SIG-1.

    Runs the shared :func:`finalize_interrupted_attempt` routine — the same
    routine M8 recovery calls at startup — and reports its Terminal Outcome
    for the node to return. Cancellation keeps precedence: the routine
    re-reads the issue and decides ``abandoned`` / ``cancelled`` where the
    issue was cancelled meanwhile. Where an outcome is already recorded (a
    second stop point after the first finalization) it is returned as-is.
    """
    workflow_id, digest = _workflow_pin(
        context.ledger, str(state["attempt_id"]), context.effective_configuration
    )
    result = finalize_interrupted_attempt(
        ledger=context.ledger,
        tracker=context.tracker,
        workspace_path=context.workspace,
        attempt_id=str(state["attempt_id"]),
        issue_number=int(state["issue_number"]),
        attempt_number=int(state["attempt_number"]),
        issue_title=str(state.get("issue_title") or ""),
        remote_url=context.remote_url,
        base_revision=str(state.get("base_revision") or ""),
        workflow_id=workflow_id,
        configuration_digest=digest,
        selection_label=context.tracker.selection_label,
    )
    return {"outcome": result.outcome, "reason": result.reason}


def _shutdown_diversion(
    state: AttemptState, context: AttemptContext
) -> dict[str, object] | None:
    """The pre-Intent SIGTERM diversion, or ``None`` where none applies (§11).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §11 (SIGTERM before
    the Delivery Intent ends the Attempt ``failed`` / ``interrupted``, or
    ``abandoned`` where the issue was cancelled meanwhile) and §14 (the
    Implementation Outcome → Terminal Outcome table).

    MVP row ids: P-CAN-3, REC-SIG-1, REC-SIG-2.

    Returns the shared finalization's outcome where SIGTERM was observed and
    no Terminal Outcome is recorded yet, else ``None`` so the node runs its
    ordinary logic. Only pre-Intent nodes call this: ``delivery_intent``,
    ``publish`` and ``terminal_writes`` never divert, so a signal after the
    Delivery Intent lets publication complete as ``delivered`` (REC-SIG-2).
    """
    if not is_shutdown_requested():
        return None
    if context.ledger.get_outcome(str(state["attempt_id"])) is not None:
        return None
    return _finalize_shutdown(state, context)


def run_step(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§8 + §9 + §11: run the ordered Workflow Step list, then decide its outcome.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (one Workflow
    Step's conversation with its model, its tools and its exit, per step: the
    Pinned Prefix, then the forced activation call; a fresh step subgraph per
    step with its own message list — the next step opens without the previous
    step's history, by construction; two step kinds: ``skill: implement``
    opens with the forced ``activate_implement``, a ``prompt_file`` step
    opens with a forced ``activate_step`` whose result is the prompt file's
    bytes, verbatim and exempt from result capping; positional exit tools:
    ``report_step`` (``done`` or ``cannot-continue``, plus a justification
    bounded in size) in every non-final step and never in the final one,
    ``report_completion`` only in the final step; the step boundary composes
    a Continuation Snapshot with the handoff mechanism carrying the
    justification as a bounded field; the working tree carries over and the
    work-node budgets are restored; stopping the list: a non-final step
    ending ``cannot-continue`` stops the list, a non-final step ending in
    silence after its continuations stops the list, a provider failure after
    its retries in any step stops the list, and later steps never start —
    the Attempt continues to the fixed tail; Exit — silence is answered, not
    obeyed: a response with no tool calls and no accepted exit draws one
    deterministic continuation composed from the unmet obligations, budget
    two per work node, monotonic and never reset by progress; once spent the
    node ends ``completion not established`` / ``undeclared``), §9 (the
    runtime owns the evidence: ``test_targeted`` is the sole writer of the
    per-Attempt evidence dir and records Criterion Evidence;
    ``report_completion`` is the only non-exceptional exit of the final step
    with a per-Attempt schema; an unbacked claim is returned naming every
    failing entry, never a progress event; after two returns the third call
    is force-accepted; an explicit ``uncovered`` is accepted as
    ``criteria-uncovered``; exactly one Implementation Outcome is decided
    from the claim and Criterion Evidence alone; only ``verified
    completion`` may use PASS wording) and §11 (an oversized tool result is
    capped with a slice pointer, activation results exempt; the assistant
    turn is carried back verbatim; cache breakpoints are applied; a Pinned
    Prefix that cannot fit with its overhead refuses the Attempt before any
    model work; Progress Gates and the verification reserve are
    Attempt-wide meters observed after every model response and every tool
    result; five consecutive responses without a progress event record one
    soft stall; at 70% of any meter the ledger records one durable Gate and
    at 80% one reserve entry; the phase only moves forward; a ceiling stops
    the loop inside the node; the category follows the moment of
    acceptance, not the moment of measurement), and ADR 0014. Where the
    Pinned Model configures a context window, a context crossing is a
    durable handoff into a fresh node (contract §11, ADR 0013): before a
    request would exceed the usable threshold the work node stops with no
    over-threshold call and no split Exchange Unit; one bounded handoff
    node — one model invocation, no tools, its own output cap — produces a
    versioned redacted Continuation Snapshot capped at 32 KiB, persisted by
    SHA-256 digest, recorded in the Run Ledger and charged to the
    Attempt-wide meters; the fresh node reopens with the intact Pinned
    Prefix, the snapshot in place of history and the same forced
    activation; a Context-Window Handoff inside a ``prompt_file`` step
    reopens it with a forced ``activate_step`` carrying identical bytes;
    both work-node budgets restored and Attempt-wide meters unbroken. A
    malformed, incomplete, over-limit, unpersistable or unverifiable
    snapshot — or a fresh opening that cannot fit — ends the Attempt
    ``failed`` / ``handoff-failure`` with a redacted failure artifact and no
    continuation. A provider failure after its retries, in any step, ends
    ``failed`` / ``infrastructure-failure`` with class ``provider``: the
    retries hit the same Pinned Model, with no model swap and no automatic
    new Attempt.

    MVP row ids: C-SKL-1,
    C-SKL-2, G-HDR-1, L2-2, L3-IMP-1, L3-IMP-4, L3-IMP-5, L3-IMP-10,
    L3-IMP-12, L3-IMP-13, L3-IMP-15, L3-IMP-16, L3-IMP-17, L3-IMP-18,
    L3-IMP-20, L3-IMP-21, L3-IMP-23, L3-IMP-24,
    L3-IMP-25, L3-IMP-26, L3-IMP-30, L3-IMP-31, C-CTX-1, P-CTX-1, C-IMP-1,
    L3-IMP-28, L3-IMP-29, W-STP-1, W-STP-2, W-STP-4, W-STP-5, G-STP-1,
    C-STP-1, C-ERR-1, C-LIM-1, P-LIM-1, L3-IMP-7,
    P-CAN-1, P-CAN-3, W-STP-3, G-REV-3, REC-SIG-1.

    A Readiness refusal recorded above passes straight through with no
    model call. Entering with no Seam Set in state is refused as an
    internal invariant violation (L3-IMP-11): ``failed``, non-transient,
    with no model call.

    Cancellation (contract §11, P-CAN-1, W-STP-3, G-REV-3) is checked at
    every step boundary and after every work node: a stop found before the
    Delivery Intent ends the Attempt ``abandoned`` / ``cancelled`` with no
    pull request, and the Attempt still continues to the fixed tail, so the
    Delivery Snapshot commits and pushes the work. The in-tool Gate's stop
    (``review_state.stopped_at_gate``) is read the same way after each work
    node. SIGTERM (contract §11, P-CAN-3, REC-SIG-1) is observed at the
    same points — and, through ``stop_requested``, after every model
    response and every tool result inside the work node: a stop found
    before the Delivery Intent takes the claim-confirmed interrupted path
    instead (local commits pushed fast-forward, the dirty tree never
    committed), ending ``failed`` / ``interrupted``, or ``abandoned``
    where the issue was cancelled meanwhile.

    Each step opens with the Pinned Prefix — the Attempt Header the Worker
    authors, then the Target Issue's title and body (never comments) — plus,
    past the first step, the previous boundary's Continuation Snapshot, and
    the first request forces the step's activation call through
    ``tool_choice``. Skill bodies arrive only as activation results (never
    capped). Before any model call, the opening (with tool-schema and
    request overhead) is checked against the Pinned Model's usable context
    (L3-IMP-13): over it, the Attempt ends ``failed`` / ``handoff-failure``
    with an explanatory reason, no model call and no partial prefix.

    The Acceptance Criteria list is the runtime's enumeration from readiness
    (contract §7, L3-IMP-26), held in state — never the model's — and the
    per-Attempt ``report_completion`` schema is generated from exactly that
    list, so a claim answering fewer criteria cannot be expressed. The
    profile is read at the Base Revision and pinned as the Validation
    Contract, so the ``test_targeted`` template is the pinned one even
    where the working tree edits the profile. The toolset carries the
    runtime-owned ``test_targeted`` tool and the positional exit tool:
    ``report_step`` in non-final steps, ``report_completion`` in the final
    step whose schema lists exactly the Attempt's ``AC-n`` ids. The step
    loop returns unbacked claims inside the work node (the claim-rejection
    budget lives here and is restored on a fresh node). After the final
    step, the claim is read from the messages and the Implementation
    Outcome is decided from that claim, the force-accept flag and the
    runtime-owned Criterion Evidence alone: ``verified completion`` (a diff
    plus passing evidence for every criterion) ends ``delivered`` /
    ``verified-completion``; a force-accepted claim with a diff ends
    ``failed`` / ``unverified`` (``implemented but unverified``); a
    force-accepted claim without one ends ``failed`` /
    ``claims-unverified``; no diff with complete passing evidence ends
    ``failed`` / ``already-satisfied`` (L3-IMP-10); a turn with no tool
    calls never yields an outcome on its own — after two deterministic
    continuations the node ends ``completion not established`` /
    ``undeclared`` whether or not a diff exists (L3-IMP-24); a non-final
    step ending ``cannot-continue`` ends ``completion not established`` /
    ``step-cannot-continue`` (W-STP-1); a non-final step ending in silence
    after its continuations ends ``completion not established`` /
    ``undeclared`` (C-STP-1); anything else is ``completion not
    established`` (``failed`` / ``criteria-uncovered`` or ``failed`` /
    ``no-completion-evidence``, L3-IMP-20).
    """
    if state.get("outcome"):
        return {}
    if not state.get("seam_set"):
        return {
            "outcome": FAILED,
            "reason": (
                "missing-readiness-fact: seam-set "
                "(internal invariant violation: implement entered with no Seam Set in state)"
            ),
        }
    context = AttemptContext.from_config(config)
    issue_title = str(state.get("issue_title", ""))
    issue_body = str(state.get("issue_body", ""))
    # The Acceptance Criteria list is the runtime's enumeration from readiness
    # (contract §7, L3-IMP-26), held in state — never the model's. Prefer the
    # readiness-held list so the report_completion schema is generated from
    # exactly the Attempt's held criteria; fall back to enumerating from the
    # body only where state carries none (never in a real Attempt).
    held = state.get("acceptance_criteria")
    if isinstance(held, list) and held:
        acceptance_criteria = tuple(
            (str(item.get("id", "")), str(item.get("text", "")))
            for item in held
            if isinstance(item, dict)
        )
    else:
        criteria_texts = enumerate_acceptance_criteria(issue_body)
        acceptance_criteria = tuple(
            (f"AC-{index}", text) for index, text in enumerate(criteria_texts, 1)
        )
    criteria_ids = tuple(criterion_id for criterion_id, _text in acceptance_criteria)
    seam_set = derive_seam_set(issue_title, issue_body)
    branch = attempt_branch_name(
        int(state["issue_number"]), int(state["attempt_number"]), issue_title
    )
    base_revision = _workspace_base_revision(context.workspace)
    state_revision = str(state.get("base_revision") or base_revision)
    profile_text = read_file_at_revision(
        context.workspace, state_revision, PROFILE_RELPATH
    )
    if profile_text is None:
        return {
            "outcome": FAILED,
            "reason": "no-completion-evidence",
        }
    parsed = parse_profile_yaml(profile_text)
    if not isinstance(parsed, ProjectProfile):
        return {
            "outcome": FAILED,
            "reason": "no-completion-evidence",
        }
    profile = parsed
    # The M1 CLI-derived settings carry ``target_language``; an M5 Effective
    # Configuration record (contract §2.5) carries no language, so the Attempt
    # Header answers with the Validation Contract's own language instead
    # (P-CMD-2: the profile is read at the configured base branch's Base
    # Revision).
    configured_language = context.effective_configuration.get("target_language")
    target_language = (
        str(configured_language) if configured_language else profile.language
    )
    header = compose_mvp_attempt_header(
        attempt_id=str(state["attempt_id"]),
        base_revision=base_revision,
        branch=branch,
        acceptance_criteria=acceptance_criteria,
        seam_set=seam_set,
        target_language=target_language,
    )
    opening = build_step_opening_messages(header, issue_title, issue_body)
    store: ArtifactStore
    if context.artifact_store is not None:
        store = context.artifact_store
    elif context.evidence_dir is not None:
        store = FilesystemArtifactStore(root=context.evidence_dir / "tool-results")
    else:
        store = InMemoryArtifactStore()
    if context.evidence_dir is not None:
        targeted_evidence_dir = context.evidence_dir
        targeted_evidence_dir.mkdir(parents=True, exist_ok=True)
    else:
        targeted_evidence_dir = Path(tempfile.mkdtemp(prefix="coding-agent-targeted-"))
    steps: tuple[WorkflowStep, ...] = tuple(context.steps) or DEFAULT_STEPS
    if not steps:
        return {
            "outcome": FAILED,
            "reason": (
                "missing-readiness-fact: workflow-steps "
                "(internal invariant violation: run_step entered with no Workflow Steps)"
            ),
        }

    def _silence_text(sequence_number: int) -> str:
        """Compose one deterministic continuation (contract §8, ADR 0014).

        Pure function of the unmet obligations — the Acceptance Criteria with
        no passing Criterion Evidence (with no accepted exit, every criterion
        the Attempt holds), whether a candidate diff exists, and which
        targeted tests have run — never a model call. Deterministic for a
        given state: sorted ids, fixed wording, no timestamps.
        """
        uncovered = tuple(criterion_id for criterion_id, _text in acceptance_criteria)
        executed = tuple(
            sorted(
                (
                    test_id,
                    bool(context.criterion_evidence.latest_result(test_id) is True),
                )
                for test_id in context.criterion_evidence.executed_ids()
            )
        )
        return compose_silence_continuation(
            sequence_number=sequence_number,
            acceptance_criteria=acceptance_criteria,
            uncovered_ids=uncovered,
            has_diff=_workspace_has_diff(context.workspace, state_revision),
            executed_tests=executed,
        )

    def _build_tools_for_step(
        step: WorkflowStep,
        *,
        is_final: bool,
        claim_sink: dict[str, object],
        claim_record: ClaimRecord,
        step_report_record: StepReportRecord,
    ) -> tuple[BaseTool, ...]:
        """Build one step's fresh tool set with its positional exit (§8, W-STP-2).

        Where the Attempt carries a remote and an Agent Identity, the
        ``activate_code_review`` tool runs the review-snapshot push first
        and then the two reviewer seats (§10, §13, §18, L3-DEL-19, L3-DEL-20,
        P-DEL-4, C-REV-1, L3-REV-1, L3-REV-2, G-REV-1, G-REV-2, W-STP-6);
        without either, the tool set stays the plain skill-body activation
        so earlier rows bind without a remote. The push redacts the
        credential (``review_redact``) from every failure. The in-tool Gate
        in front of the snapshot push re-reads the issue through the
        Attempt's tracker (§11, G-REV-3): a stop found there ends the
        Attempt at that Gate, keeping whatever is already pushed.
        """
        review_workspace: Workspace | None = None
        if context.remote_url is not None and context.agent_identity is not None:
            review_workspace = _workspace_on_current_branch(
                context.workspace, base_revision=state_revision
            )
        review_attempt_id = str(state["attempt_id"])
        review_issue_number = int(state["issue_number"])
        review_attempt_number = int(state["attempt_number"])
        review_tracker = context.tracker
        review_selection_label = context.tracker.selection_label
        if is_final:
            return build_implement_step_tools(
                context.workspace,
                skills_dir=context.skills_dir,
                profile=profile,
                command_runner=(
                    context.command_runner
                    if context.command_runner is not None
                    else SubprocessCommandRunner()
                ),
                evidence_dir=targeted_evidence_dir,
                criterion_evidence=context.criterion_evidence,
                criteria_ids=criteria_ids,
                claim_sink=claim_sink,
                claim_record=claim_record,
                artifact_store=store,
                inline_limit=context.result_cap_limit,
                attempt_id=str(state["attempt_id"]),
                redactions=redactions,
                is_final=True,
                prompt_bytes=step.prompt_bytes,
                review_ledger=context.ledger,
                review_workspace=review_workspace,
                review_attempt_id=review_attempt_id,
                review_issue_number=review_issue_number,
                review_attempt_number=review_attempt_number,
                review_issue_title=issue_title,
                review_identity=context.agent_identity,
                review_remote_url=context.remote_url,
                review_state=context.review_state,
                review_model=context.model,
                review_price=context.price,
                review_issue_body=issue_body,
                review_base_revision=state_revision,
                review_redact=context.review_redact,
                review_tracker=review_tracker,
                review_selection_label=review_selection_label,
            )
        return build_implement_step_tools(
            context.workspace,
            skills_dir=context.skills_dir,
            profile=profile,
            command_runner=(
                context.command_runner
                if context.command_runner is not None
                else SubprocessCommandRunner()
            ),
            evidence_dir=targeted_evidence_dir,
            criterion_evidence=context.criterion_evidence,
            criteria_ids=criteria_ids,
            artifact_store=store,
            inline_limit=context.result_cap_limit,
            attempt_id=str(state["attempt_id"]),
            redactions=redactions,
            is_final=False,
            prompt_bytes=step.prompt_bytes,
            step_report_record=step_report_record,
            review_ledger=context.ledger,
            review_workspace=review_workspace,
            review_attempt_id=review_attempt_id,
            review_issue_number=review_issue_number,
            review_attempt_number=review_attempt_number,
            review_issue_title=issue_title,
            review_identity=context.agent_identity,
            review_remote_url=context.remote_url,
            review_state=context.review_state,
            review_model=context.model,
            review_price=context.price,
            review_issue_body=issue_body,
            review_base_revision=state_revision,
            review_redact=context.review_redact,
            review_tracker=review_tracker,
            review_selection_label=review_selection_label,
        )

    progress = (
        context.progress
        if context.progress is not None
        else StepProgress(
            str(state["attempt_id"]),
            context.ceilings,
            ledger=context.ledger,
        )
    )
    started_at = time.monotonic()

    def _elapsed() -> float:
        """Wall clock from the Attempt's ledger opening (§11, L3-IMP-7)."""
        return _attempt_elapsed_seconds(
            ledger=context.ledger,
            attempt_id=str(state["attempt_id"]),
            started_at=started_at,
        )

    window = context.context_window
    redactions: Mapping[str, str] = dict(context.redactions or {})
    # Each step — and each Context-Window Handoff within a step — rebuilds
    # its tools and records fresh, so the new work node opens with both
    # budgets restored (contract §8, §9, L3-IMP-31). The Attempt-wide meters
    # (``progress`` and ``cumulative``) run on unbroken across every step.
    cumulative = UsageTotals()
    handoff_total = 0
    boundary_snapshot: ContinuationSnapshot | None = None
    selection_label = context.tracker.selection_label
    issue_number = int(state["issue_number"])
    for step_index, step in enumerate(steps):
        is_final = step_index == len(steps) - 1
        next_step_id = None if is_final else steps[step_index + 1].step_id
        try:
            check_cancellation(
                tracker=context.tracker,
                issue_number=issue_number,
                selection_label=selection_label,
            )
        except GateStop as exc:
            return {"outcome": ABANDONED, "reason": exc.reason}
        diverted = _shutdown_diversion(state, context)
        if diverted is not None:
            return diverted
        claim_sink: dict[str, object] = {}
        claim_record = ClaimRecord()
        step_report_record = StepReportRecord()
        tools = _build_tools_for_step(
            step,
            is_final=is_final,
            claim_sink=claim_sink,
            claim_record=claim_record,
            step_report_record=step_report_record,
        )
        if boundary_snapshot is None:
            step_base: list[BaseMessage] = list(opening)
        else:
            step_base = [*opening, continuation_message(boundary_snapshot)]
        if window is not None:
            refusal = check_opening_fits_context(
                list(step_base), list(tools), window
            )
            if refusal is not None:
                if step_index == 0 and boundary_snapshot is None:
                    return {"outcome": FAILED, "reason": f"handoff-failure: {refusal}"}
                return _record_handoff_failure(
                    ledger=context.ledger,
                    store=store,
                    attempt_id=str(state["attempt_id"]),
                    sequence=handoff_total + 1,
                    error=refusal,
                    excerpt="",
                    context_estimate_tokens=estimate_context_tokens(
                        list(step_base), [], list(tools)
                    ),
                    node_cap_tokens=window.handoff_token_cap,
                    cumulative=cumulative,
                    elapsed_seconds=_elapsed(),
                    redactions=redactions,
                )
        try:
            messages: list[BaseMessage] = []
            handoffs = 0
            current_opening: list[BaseMessage] = list(step_base)
            while True:
                if handoffs > 0:
                    claim_sink = {}
                    claim_record = ClaimRecord()
                    step_report_record = StepReportRecord()
                    tools = _build_tools_for_step(
                        step,
                        is_final=is_final,
                        claim_sink=claim_sink,
                        claim_record=claim_record,
                        step_report_record=step_report_record,
                    )
                    assert window is not None
                    fresh_refusal = check_opening_fits_context(
                        list(current_opening), list(tools), window
                    )
                    if fresh_refusal is not None:
                        return _record_handoff_failure(
                            ledger=context.ledger,
                            store=store,
                            attempt_id=str(state["attempt_id"]),
                            sequence=handoff_total + 1,
                            error=fresh_refusal,
                            excerpt="",
                            context_estimate_tokens=estimate_context_tokens(
                                list(current_opening), [], list(tools)
                            ),
                            node_cap_tokens=window.handoff_token_cap,
                            cumulative=cumulative,
                            elapsed_seconds=_elapsed(),
                            redactions=redactions,
                        )
                node = run_work_node(
                    context.model,
                    tools,
                    current_opening,
                    forced_tool=step.forced_tool,
                    result_cap_limit=context.result_cap_limit,
                    artifact_store=store,
                    cache_breakpoints=context.cache_breakpoints,
                    max_silence_continuations=MAX_SILENCE_CONTINUATIONS,
                    silence_message_for_sequence=_silence_text,
                    claim_record=claim_record if is_final else None,
                    step_report_record=None if is_final else step_report_record,
                    exit_tool_name=(
                        "report_completion" if is_final else "report_step"
                    ),
                    progress=progress,
                    ceilings=context.ceilings,
                    workspace=context.workspace,
                    criterion_evidence=context.criterion_evidence,
                    started_at=started_at,
                    context_window=window,
                    usage_baseline=cumulative,
                    pending_usage=lambda: context.review_state.pending_review_usage,
                    price=context.price,
                    stop_requested=is_shutdown_requested,
                )
                if context.review_state.stopped_at_gate is not None:
                    # The in-tool Gate in front of the review-snapshot push
                    # saw a stop (§11, G-REV-3): the Attempt ends here, at
                    # this Gate, keeping whatever is already pushed.
                    # Cancellation ends ``abandoned`` / ``cancelled``;
                    # SIGTERM ends ``failed`` / ``interrupted`` (P-CAN-3).
                    gate_reason = context.review_state.stopped_at_gate
                    if gate_reason == CANCELLED_REASON:
                        return {"outcome": ABANDONED, "reason": gate_reason}
                    return {"outcome": FAILED, "reason": gate_reason}
                if context.review_state.failure == WORKFLOWS_FAILURE_REASON:
                    # A review-snapshot push refusal (§10, §13, L3-ERR-9,
                    # L3-ERR-10): a tree touching ``.github/workflows/`` or
                    # a rejected push ends the Attempt ``failed`` /
                    # ``workflows-path`` here, before any boundary snapshot
                    # or Implementation Outcome is composed.
                    return {"outcome": FAILED, "reason": WORKFLOWS_FAILURE_REASON}
                cumulative = cumulative + _node_usage(node.messages, context.price)
                pending_review = context.review_state.take_pending_review_usage()
                if pending_review != UsageTotals():
                    # Reviewer conversations count against the Attempt-wide
                    # meters, with no reserved review budget (§11, W-STP-6):
                    # fold the seats' spend into the cumulative totals the
                    # progress Gates and the reserve observe. Reviews never
                    # record progress events, so only the budget is observed.
                    cumulative = cumulative + pending_review
                    progress.observe_budget(
                        BudgetSnapshot(
                            elapsed_seconds=_elapsed(),
                            usage=cumulative,
                        )
                    )
                if not node.handoff_requested:
                    messages = node.messages
                    break
                assert window is not None
                if handoffs >= MAX_HANDOFFS_PER_STEP:
                    return _record_handoff_failure(
                        ledger=context.ledger,
                        store=store,
                        attempt_id=str(state["attempt_id"]),
                        sequence=handoff_total + 1,
                        error=(
                            "handoff budget exhausted after "
                            f"{MAX_HANDOFFS_PER_STEP} handoffs; refusing a further continuation"
                        ),
                        excerpt="",
                        context_estimate_tokens=node.context_estimate_tokens,
                        node_cap_tokens=window.handoff_token_cap,
                        cumulative=cumulative,
                        elapsed_seconds=_elapsed(),
                        redactions=redactions,
                    )
                sequence = handoff_total + 1
                prompt = build_handoff_prompt(
                    attempt_id=str(state["attempt_id"]),
                    base_revision=state_revision,
                    branch=branch,
                    commit=_workspace_base_revision(context.workspace),
                    changed_files=_handoff_changed_files(context.workspace),
                    commands=_handoff_commands(context.criterion_evidence),
                    validation_evidence=_handoff_validation_evidence(
                        context.criterion_evidence
                    ),
                )
                try:
                    snapshot, provider_in, provider_out, handoff_cost = execute_handoff_turn(
                        context.model,
                        prompt,
                        redactions=redactions,
                        handoff_token_cap=window.handoff_token_cap,
                        price=context.price,
                    )
                except HandoffFailure as exc:
                    return _record_handoff_failure(
                        ledger=context.ledger,
                        store=store,
                        attempt_id=str(state["attempt_id"]),
                        sequence=sequence,
                        error=str(exc),
                        excerpt=exc.excerpt,
                        context_estimate_tokens=node.context_estimate_tokens,
                        node_cap_tokens=window.handoff_token_cap,
                        cumulative=cumulative,
                        elapsed_seconds=_elapsed(),
                        redactions=redactions,
                    )
                try:
                    artifact_ref = persist_snapshot(store, snapshot)
                except HandoffFailure as exc:
                    return _record_handoff_failure(
                        ledger=context.ledger,
                        store=store,
                        attempt_id=str(state["attempt_id"]),
                        sequence=sequence,
                        error=str(exc),
                        excerpt=snapshot.to_json(),
                        context_estimate_tokens=node.context_estimate_tokens,
                        node_cap_tokens=window.handoff_token_cap,
                        cumulative=cumulative,
                        elapsed_seconds=_elapsed(),
                        redactions=redactions,
                    )
                handoff_total_tokens = provider_in + provider_out
                if handoff_total_tokens > 0:
                    handoff_cost_in = handoff_cost * provider_in / handoff_total_tokens
                    handoff_cost_out = handoff_cost * provider_out / handoff_total_tokens
                else:
                    handoff_cost_in = 0.0
                    handoff_cost_out = 0.0
                cumulative = cumulative + UsageTotals(
                    tokens=handoff_total_tokens,
                    effective_tokens=handoff_total_tokens,
                    cost_usd=handoff_cost,
                    input_tokens=provider_in,
                    output_tokens=provider_out,
                    cost_input_usd=handoff_cost_in,
                    cost_output_usd=handoff_cost_out,
                )
                handoff_snapshot = BudgetSnapshot(
                    elapsed_seconds=_elapsed(), usage=cumulative
                )
                progress.observe_budget(handoff_snapshot)
                context.ledger.record(
                    str(state["attempt_id"]),
                    "handoff",
                    handoff_snapshot,
                    detail=f"handoff {sequence}: continuation snapshot {snapshot.digest}",
                    artifact_ref=artifact_ref,
                    payload=handoff_event_payload(
                        snapshot=snapshot,
                        context_estimate_tokens=node.context_estimate_tokens,
                        node_cap_tokens=window.handoff_token_cap,
                        provider_input_tokens=provider_in,
                        provider_output_tokens=provider_out,
                        cumulative_tokens=cumulative.tokens,
                        cumulative_effective_tokens=cumulative.effective_tokens,
                        cumulative_cost_usd=cumulative.cost_usd,
                        cumulative_tool_calls=cumulative.tool_calls,
                    ),
                )
                handoffs += 1
                handoff_total += 1
                current_opening = [*step_base, continuation_message(snapshot)]
        except ModelError as exc:
            # A provider failure after its retries, in any step (contract
            # §8, C-ERR-1): the retries hit the same Pinned Model —
            # ``invoke_with_retry`` takes exactly one model, so there is no
            # model swap and no automatic new Attempt — and the list stops.
            return {
                "outcome": FAILED,
                "reason": f"infrastructure-failure: provider {str(exc)[:500]}",
            }
        diverted = _shutdown_diversion(state, context)
        if diverted is not None:
            # SIGTERM observed at a step stop point before the Delivery
            # Intent (contract §11, P-CAN-3, REC-SIG-1): the claim-confirmed
            # interrupted path, not the claim-state cutoff below — even
            # where the node earned a decision, the Delivery Intent has
            # not run yet. Cancellation keeps precedence inside the shared
            # finalization's re-read, and a stop already recorded at the
            # in-tool Gate above keeps its own reason.
            return diverted
        if not is_final:
            status: str | None = None
            justification = ""
            if step_report_record.report is not None:
                status = step_report_record.report.status
                justification = step_report_record.report.justification
            elif has_step_report(messages):
                try:
                    reported_args = step_report_args(messages)
                except AssertionError:
                    reported_args = {}
                candidate_status = reported_args.get("status")
                candidate_justification = reported_args.get("justification", "")
                if candidate_status in (
                    REPORT_STEP_DONE,
                    REPORT_STEP_CANNOT_CONTINUE,
                ) and isinstance(candidate_justification, str):
                    assert isinstance(candidate_status, str)
                    status = candidate_status
                    justification = candidate_justification
            if status is None:
                # No accepted exit: a crossed ceiling ends the Attempt
                # ``failed-limit`` naming the limit (contract §11, hard
                # ceilings; C-LIM-1, P-LIM-1, L3-IMP-7); the verification
                # reserve or a soft stall ends saved partial work with a
                # candidate diff (contract §11); silence after its
                # continuations ends ``completion not established`` /
                # ``undeclared`` (C-STP-1), whether or not a diff exists.
                if progress.ceiling is not None:
                    _flush_ceiling_usage(
                        progress=progress,
                        cumulative=cumulative,
                        elapsed_seconds=_elapsed(),
                    )
                    return {"outcome": FAILED_LIMIT, "reason": progress.ceiling}
                if (
                    progress.phase == "verification_reserve"
                    or progress.soft_stalled
                ):
                    if _workspace_has_diff(context.workspace, state_revision):
                        return {"outcome": FAILED, "reason": "saved-partial-work"}
                    return {"outcome": FAILED, "reason": SILENCE_EXHAUSTED_REASON}
                return {"outcome": FAILED, "reason": SILENCE_EXHAUSTED_REASON}
            if status == REPORT_STEP_CANNOT_CONTINUE:
                # The list stops; later steps never start (contract §8,
                # W-STP-1). The Attempt continues to the fixed tail, so any
                # diff stays in the workspace for ``delivery_snapshot``.
                return {"outcome": FAILED, "reason": "step-cannot-continue"}
            assert next_step_id is not None
            try:
                boundary_snapshot = _compose_step_boundary_snapshot(
                    attempt_id=str(state["attempt_id"]),
                    base_revision=state_revision,
                    branch=branch,
                    commit=_workspace_base_revision(context.workspace),
                    changed_files=_handoff_changed_files(context.workspace),
                    commands=_handoff_commands(context.criterion_evidence),
                    validation_evidence=_handoff_validation_evidence(
                        context.criterion_evidence
                    ),
                    justification=justification,
                    next_step_id=next_step_id,
                    redactions=redactions,
                )
            except HandoffFailure as exc:
                cap = window.handoff_token_cap if window is not None else 0
                return _record_handoff_failure(
                    ledger=context.ledger,
                    store=store,
                    attempt_id=str(state["attempt_id"]),
                    sequence=handoff_total + 1,
                    error=str(exc),
                    excerpt=justification[:2000],
                    context_estimate_tokens=estimate_context_tokens(
                        list(step_base), [], list(tools)
                    ),
                    node_cap_tokens=cap,
                    cumulative=cumulative,
                    elapsed_seconds=_elapsed(),
                    redactions=redactions,
                )
            try:
                boundary_ref = persist_snapshot(store, boundary_snapshot)
            except HandoffFailure as exc:
                cap = window.handoff_token_cap if window is not None else 0
                return _record_handoff_failure(
                    ledger=context.ledger,
                    store=store,
                    attempt_id=str(state["attempt_id"]),
                    sequence=handoff_total + 1,
                    error=str(exc),
                    excerpt=boundary_snapshot.to_json(),
                    context_estimate_tokens=estimate_context_tokens(
                        list(step_base), [], list(tools)
                    ),
                    node_cap_tokens=cap,
                    cumulative=cumulative,
                    elapsed_seconds=_elapsed(),
                    redactions=redactions,
                )
            context.ledger.record(
                str(state["attempt_id"]),
                STEP_BOUNDARY_KIND,
                BudgetSnapshot(
                    elapsed_seconds=_elapsed(), usage=cumulative
                ),
                detail=(
                    f"step boundary {step.step_id} -> {next_step_id}: "
                    f"continuation snapshot {boundary_snapshot.digest}"
                ),
                artifact_ref=boundary_ref,
                payload=step_boundary_event_payload(
                    snapshot=boundary_snapshot,
                    step_id=step.step_id,
                    step_index=step_index,
                    next_step_id=next_step_id,
                ),
            )
            continue
        cutoff = (
            progress.ceiling is not None
            or progress.phase == "verification_reserve"
            or progress.soft_stalled
        )
        if claim_record.accepted_raw is not None:
            # The category follows the moment of acceptance, not the moment of
            # measurement (contract §9, L3-IMP-30): a ceiling crossed after an
            # accepted and validated claim still yields the outcome the claim
            # earned, even where the loop stopped on a ceiling.
            raw_accepted: dict[str, object] = dict(claim_record.accepted_raw)
            accepted_claim = normalize_claim(raw_accepted, criteria_ids)
            has_diff = _workspace_has_diff(context.workspace, state_revision)
            outcome, reason = decide_implementation_outcome(
                accepted_claim,
                context.criterion_evidence,
                has_diff=has_diff,
                force_accepted=claim_record.force_accepted,
            )
            state_claim = {
                criterion_id: (list(covering) if covering is not None else None)
                for criterion_id, covering in accepted_claim.items()
            }
            if outcome == "verified completion":
                return {
                    "outcome": DELIVERED,
                    "reason": reason,
                    "claim": state_claim,
                    "claim_force_accepted": claim_record.force_accepted,
                }
            return {
                "outcome": FAILED,
                "reason": reason,
                "claim": state_claim,
                "claim_force_accepted": claim_record.force_accepted,
            }
        if progress.ceiling is not None:
            # Cut short by a crossed ceiling with no accepted claim
            # (contract §11, hard ceilings): the Attempt ends ``failed-limit``
            # naming the limit, with or without a candidate diff (the
            # Implementation Outcome → Terminal Outcome table maps both
            # ``saved partial work`` and ``completion not established`` to
            # ``failed-limit`` when a ceiling ended the stage). Usage is
            # flushed first so the spend is durable (C-LIM-1).
            _flush_ceiling_usage(
                progress=progress,
                cumulative=cumulative,
                elapsed_seconds=_elapsed(),
            )
            return {"outcome": FAILED_LIMIT, "reason": progress.ceiling}
        if cutoff:
            # Cut short by the verification reserve or a soft stall with no
            # accepted claim (contract §11, L3-IMP-16, L3-IMP-17, L3-IMP-18,
            # L3-IMP-30): saved partial work with a candidate diff,
            # completion not established / undeclared without one.
            if _workspace_has_diff(context.workspace, state_revision):
                return {"outcome": FAILED, "reason": "saved-partial-work"}
            return {"outcome": FAILED, "reason": SILENCE_EXHAUSTED_REASON}
        if not has_report_completion(messages):
            # Silence is answered, not obeyed (contract §8, ADR 0014, L3-IMP-24):
            # a turn with no tool calls never yields an Implementation Outcome on
            # its own. After two continuations the node ends ``completion not
            # established`` / ``undeclared``, whether or not a diff exists,
            # because silence alone is not an external cause.
            return {"outcome": FAILED, "reason": SILENCE_EXHAUSTED_REASON}
        raw_claim: dict[str, object] | None = None
        if has_report_completion(messages):
            raw_claim = report_completion_args(messages)
        elif claim_sink:
            raw_claim = dict(claim_sink)
        if raw_claim is None and claim_record.accepted_raw is not None:
            raw_claim = dict(claim_record.accepted_raw)
        claim = (
            normalize_claim(raw_claim, criteria_ids) if raw_claim is not None else None
        )
        has_diff = _workspace_has_diff(context.workspace, state_revision)
        outcome, reason = decide_implementation_outcome(
            claim,
            context.criterion_evidence,
            has_diff=has_diff,
            force_accepted=claim_record.force_accepted,
        )
        if claim is not None:
            state_claim = {
                criterion_id: (list(covering) if covering is not None else None)
                for criterion_id, covering in claim.items()
            }
            if outcome == "verified completion":
                return {
                    "outcome": DELIVERED,
                    "reason": reason,
                    "claim": state_claim,
                    "claim_force_accepted": claim_record.force_accepted,
                }
            return {
                "outcome": FAILED,
                "reason": reason,
                "claim": state_claim,
                "claim_force_accepted": claim_record.force_accepted,
            }
        if outcome == "verified completion":
            return {"outcome": DELIVERED, "reason": reason}
        return {"outcome": FAILED, "reason": reason}
    raise AssertionError(
        "unreachable: a non-empty Workflow Step list always returns from run_step"
    )


def gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§11: the last Gate before the Delivery Snapshot (C-LIM-1, L3-IMP-7).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §6 (``gate`` is the
    Gate between steps), §8 (cancellation at a step-boundary Gate yields
    ``abandoned``) and §11 ("Hard ceilings": a crossed ceiling ends the
    Attempt ``failed-limit`` naming the limit, with usage flushed first; the
    wall clock is measured from the Attempt's ledger opening, never from
    process start, so review time counts too; a human cancels by closing the
    issue or removing the Selection Label — a Gate before the Delivery
    Intent that sees either ends the Attempt ``abandoned`` / ``cancelled``
    with no pull request, P-CAN-1, W-STP-3; SIGTERM before the Delivery
    Intent takes the claim-confirmed interrupted path instead, P-CAN-3,
    REC-SIG-1).

    MVP row ids: C-LIM-1, P-LIM-1, L3-IMP-7, P-CAN-1, P-CAN-3, W-STP-3,
    REC-SIG-1.

    Cancellation overrides any pre-Intent outcome: the Delivery Intent is
    still ahead (``delivery_intent`` runs later), so a stop found here
    diverts the Attempt to ``abandoned`` / ``cancelled`` whatever
    ``run_step`` decided. SIGTERM diverts the same way to the shared
    interrupted finalization (``failed`` / ``interrupted``, or
    ``abandoned`` where the issue was cancelled meanwhile) — but only
    where no Terminal Outcome is recorded yet, so an already-finalized
    Attempt passes through. After the Delivery Intent neither stop diverts
    anything (P-CAN-2, REC-SIG-2): this node is always before it. A
    recorded outcome with no stop passes through untouched. Otherwise the
    Attempt-wide meters are re-checked from the Run Ledger's Attempt start:
    where a ceiling is crossed (for example the wall clock crossed during
    review, L3-IMP-7) the Attempt ends ``failed-limit`` naming the limit,
    after a final usage flush. The Delivery Snapshot then still commits and
    pushes the work — except after an interrupted finalization, whose
    dirty tree is never committed.
    """
    context = AttemptContext.from_config(config)
    try:
        check_cancellation(
            tracker=context.tracker,
            issue_number=int(state["issue_number"]),
            selection_label=context.tracker.selection_label,
        )
    except GateStop as exc:
        return {"outcome": ABANDONED, "reason": exc.reason}
    diverted = _shutdown_diversion(state, context)
    if diverted is not None:
        return diverted
    if state.get("outcome"):
        return {}
    progress = context.progress
    if progress is None:
        return {}
    if progress.last_snapshot is None and progress.ceiling is None:
        return {}
    elapsed = _attempt_elapsed_seconds(
        ledger=context.ledger,
        attempt_id=str(state["attempt_id"]),
        started_at=time.monotonic(),
    )
    crossed = progress.ceiling
    if crossed is None and progress.last_snapshot is not None:
        crossed = ceiling_crossed(
            progress.last_snapshot.usage, elapsed, progress.ceilings
        )
    if crossed is None:
        return {}
    progress.ceiling = crossed
    _flush_ceiling_usage(
        progress=progress,
        cumulative=(
            progress.last_snapshot.usage
            if progress.last_snapshot is not None
            else UsageTotals()
        ),
        elapsed_seconds=elapsed,
    )
    return {"outcome": FAILED_LIMIT, "reason": crossed}


def delivery_snapshot(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: commit the candidate tree and push the branch (C-PUB-1).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Delivery
    Snapshot runs for every Implementation Outcome: when the tree differs
    from the Attempt's last commit it commits the whole tree, then pushes
    the branch whenever the branch has commits the remote lacks, through
    the §13 effects sequence) and §18 (one branch per Attempt; the
    id-based noreply identity; the ``Attempt: #<issue>/<n>`` trailer).

    MVP row ids: C-PUB-1, L3-DEL-19, L3-DEL-20, W-STP-1, C-STP-1, L3-IMP-29,
    C-LIM-1, P-LIM-1, P-CAN-3, REC-SIG-1.

    Runs for every Implementation Outcome without changing it — including
    ``failed-limit`` (contract §11: the deterministic finalization commits
    the existing edits and pushes the branch): the outcome
    ``run_step`` decided travels through untouched. ``already satisfied``
    (no diff, no branch) commits nothing, pushes nothing and creates no
    branch. A tree touching ``.github/workflows/``, or a push rejected with
    no remote branch to collide with, ends the Attempt ``failed`` /
    ``workflows-path`` (L3-ERR-9). A foreign remote ref means no push; the
    verdict on it belongs to the Publication Gate, a later M4 ticket.
    A ``workflows-path`` refusal recorded by ``run_step`` (the review
    snapshot already committed the tree and refused it) passes through
    untouched: the refused commit is never pushed (L3-ERR-9). An
    ``interrupted`` reason recorded by the SIGTERM finalization skips the
    snapshot entirely: the pushed commits are already preserved and the
    dirty tree is never committed (§11, §17, REC-SIG-1).

    Without a remote and an Agent Identity in context (scripted unit tests)
    this is a pass-through: there is nothing to push to.
    """
    context = AttemptContext.from_config(config)
    if context.remote_url is None or context.agent_identity is None:
        return {}
    if state.get("reason") == WORKFLOWS_FAILURE_REASON:
        return {}
    if state.get("reason") == INTERRUPTED_REASON:
        return {}
    diverted = _shutdown_diversion(state, context)
    if diverted is not None:
        return diverted
    workspace = _workspace_on_current_branch(
        context.workspace, base_revision=str(state.get("base_revision") or "")
    )
    try:
        publish_delivery_snapshot(
            workspace=workspace,
            ledger=context.ledger,
            attempt_id=state["attempt_id"],
            issue_number=int(state["issue_number"]),
            attempt_number=int(state["attempt_number"]),
            issue_title=str(state.get("issue_title") or ""),
            identity=context.agent_identity,
            remote_url=context.remote_url,
            redact=context.review_redact,
        )
    except DeliverySnapshotRefused as exc:
        return {"outcome": FAILED, "reason": exc.reason}
    return {}


def final_checks(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: one authoritative Validation Contract run (C-PUB-1).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (``final_checks``
    runs only for ``verified completion``: one authoritative run of
    ``test_all`` and every check against the Delivery Snapshot, outside the
    model's tool loop, with no remediation and no retry; green means every
    command exits zero, ``test_all`` executes at least one test and reports
    no failure, and every check passes; zero executed tests is missing
    evidence and therefore red; no Baseline Failure exclusion; the run uses
    the Validation Contract pinned at the Base Revision over the declared
    working area only, and both facts are recorded for disclosure).

    MVP row ids: C-PUB-1, P-DEL-2, P-DEL-8, L2-3, L2-17, L2-18, P-CAN-3,
    REC-SIG-1.

    Records its Validation Evidence in the Run Ledger and never changes the
    outcome: the Publication Gate, a later M4 ticket, decides on it. A
    SIGTERM observed first diverts to the shared interrupted finalization
    instead (§11, P-CAN-3, REC-SIG-1): no final checks run for an
    interrupted Attempt.
    """
    context = AttemptContext.from_config(config)
    diverted = _shutdown_diversion(state, context)
    if diverted is not None:
        return diverted
    if state.get("outcome") != DELIVERED:
        return {}
    attempt_id = state["attempt_id"]
    budget = (
        context.progress.last_snapshot
        if context.progress is not None
        and context.progress.last_snapshot is not None
        else BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    )
    base_revision = state.get("base_revision")
    if not base_revision:
        context.ledger.record(
            attempt_id,
            FINAL_CHECKS_KIND,
            budget,
            detail="final_checks red: no Base Revision in state",
            payload={
                "green": False,
                "contract_pinned_at_base_revision": None,
                "profile_changed_in_candidate": None,
                "working_area": None,
                "dirty_files": [],
                "commands": [],
            },
        )
        return {}
    profile_text = read_file_at_revision(
        context.workspace, base_revision, PROFILE_RELPATH
    )
    if profile_text is None:
        context.ledger.record(
            attempt_id,
            FINAL_CHECKS_KIND,
            budget,
            detail="final_checks red: no Project Profile at the Base Revision",
            payload={
                "green": False,
                "contract_pinned_at_base_revision": base_revision,
                "profile_changed_in_candidate": None,
                "working_area": None,
                "dirty_files": [],
                "commands": [],
            },
        )
        return {}
    parsed = parse_profile_yaml(profile_text)
    if not isinstance(parsed, ProjectProfile):
        context.ledger.record(
            attempt_id,
            FINAL_CHECKS_KIND,
            budget,
            detail=f"final_checks red: invalid pinned profile: {parsed!r}",
            payload={
                "green": False,
                "contract_pinned_at_base_revision": base_revision,
                "profile_changed_in_candidate": None,
                "working_area": None,
                "dirty_files": [],
                "commands": [],
            },
        )
        return {}
    profile = parsed
    if context.evidence_dir is not None:
        evidence_dir = context.evidence_dir / FINAL_CHECKS_EVIDENCE_SUBDIR
    else:
        evidence_dir = Path(tempfile.mkdtemp(prefix="coding-agent-final-checks-"))
    runner: CommandRunner = (
        context.command_runner if context.command_runner is not None else SubprocessCommandRunner()
    )
    results = run_final_checks(
        profile, runner, context.workspace / profile.working_directory, evidence_dir
    )
    green = final_checks_green(results)
    report = FinalChecksReport(
        results=results,
        green=green,
        dirty_files=dirty_working_tree_files(context.workspace),
        working_area=profile.working_directory,
        base_revision=base_revision,
        profile_changed=candidate_changed_profile(
            context.workspace,
            base_revision=base_revision,
            head_revision=head_revision(context.workspace),
        ),
    )
    context.ledger.record(
        attempt_id,
        FINAL_CHECKS_KIND,
        budget,
        detail=(
            "final_checks Validation Evidence against the Delivery Snapshot "
            f"(C-PUB-1): {'green' if green else 'red'}"
        ),
        payload=final_checks_evidence_payload(report),
    )
    return {}


def _final_checks_payload(
    ledger: RunLedger, attempt_id: str
) -> dict[str, object] | None:
    """The latest final-checks Validation Evidence payload, if one was recorded."""
    matches = [record for record in ledger.records(attempt_id) if record.kind == FINAL_CHECKS_KIND]
    if not matches:
        return None
    return dict(matches[-1].payload)


def _publication_gate_base_head(
    *, remote_url: str | None, base_branch: str, redact: str | None
) -> str | None:
    """The live base-branch head for the disclosure comparison (§12, L3-DEL-5).

    ``None`` where there is no remote in context (scripted unit tests) or the
    lookup fails: movement is then recorded as unknown rather than failing the
    Attempt. The Worker never rebases or merges whatever it finds.
    """
    if not remote_url or not base_branch:
        return None
    try:
        return remote_ref_sha(remote_url, base_branch, redact=redact)
    except GitFailure:
        return None


def _delivery_base_disclosure(
    ledger: RunLedger,
    attempt_id: str,
    *,
    remote_url: str | None,
    base_branch: str,
    base_revision: str,
    redact: str | None,
) -> tuple[str | None, bool]:
    """The base-movement comparison disclosed in the pull-request body (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (base-branch
    movement is compared and recorded for disclosure) and §15 (the body names
    the commit the work was verified against).

    MVP row ids: L3-DEL-5.

    The Publication Gate verdict's recorded comparison is reused where one
    exists, so the body discloses exactly what the gate saw; without a gate
    row (scripted runs with no remote in context) a live lookup runs instead.
    The Worker never rebases or merges whatever it finds.
    """
    gate_records = [
        record for record in ledger.records(attempt_id) if record.kind == PUBLICATION_GATE_KIND
    ]
    if gate_records:
        payload = gate_records[-1].payload
        raw_head = payload.get("base_head")
        base_head = raw_head if isinstance(raw_head, str) else None
        raw_moved = payload.get("base_moved")
        if isinstance(raw_moved, bool):
            return base_head, raw_moved
        return base_head, base_head is not None and base_head != base_revision
    base_head = _publication_gate_base_head(
        remote_url=remote_url,
        base_branch=base_branch,
        redact=redact,
    )
    return base_head, base_head is not None and base_head != base_revision


def _workflow_pin(
    ledger: RunLedger, attempt_id: str, effective_configuration: object
) -> tuple[str, str]:
    """The Workflow id and Effective Configuration digest disclosed in §15.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §2.5 (the
    pull-request body and the terminal comment show the Workflow id and the
    digest) and §15.

    MVP row ids: W-PIN-2.

    Reads the id from the M5 Effective Configuration record
    (``workflow.id``), falling back to the M1 CLI-derived settings'
    ``workflow_id`` key and then to ``implement``.
    """
    workflow_id = "implement"
    if isinstance(effective_configuration, dict):
        workflow = effective_configuration.get("workflow")
        if isinstance(workflow, dict):
            candidate = workflow.get("id")
            if isinstance(candidate, str) and candidate:
                workflow_id = candidate
        else:
            candidate = effective_configuration.get("workflow_id")
            if isinstance(candidate, str) and candidate:
                workflow_id = candidate
    try:
        digest = ledger.get_attempt(attempt_id).effective_configuration_digest
    except Exception:
        digest = "unknown"
    return workflow_id, digest


def publication_gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: the last Gate, and the verdict (P-DEL-2, P-DEL-6 … P-DEL-9, C-PUB-2).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the Publication
    Gate re-reads the Target Issue through the ``TrackerPort`` and requires,
    in order: the Implementation Outcome is ``verified completion``; no Drift;
    the review gate is met; the final checks are green; the tree is clean
    after them; the first unmet condition names the reason and every unmet
    condition is recorded for the terminal comment; base-branch movement does
    not block but is compared and recorded for disclosure; the Worker never
    rebases or merges. A SIGTERM observed first diverts to the shared
    interrupted finalization instead (§11, P-CAN-3, REC-SIG-1): no gate
    verdict is recorded for an interrupted Attempt.

    MVP row ids: P-DEL-2, P-DEL-6, P-DEL-7, P-DEL-8, P-DEL-9, C-PUB-1, C-PUB-2,
    L3-DEL-5, P-CAN-3, REC-SIG-1.

    Only ``verified completion`` reaches the gate checks: any other
    Implementation Outcome passes through untouched, so its §14 Terminal
    Outcome (``unverified``, ``already-satisfied``, ``soft-stall``,
    ``no-completion-evidence`` and friends) is preserved and no pull request
    is opened for it. For ``verified completion`` the verdict follows the
    ordered conditions Drift → review → final checks → clean tree.
    """
    context = AttemptContext.from_config(config)
    diverted = _shutdown_diversion(state, context)
    if diverted is not None:
        return diverted
    if state.get("outcome") != DELIVERED:
        return {}
    attempt_id = state["attempt_id"]
    budget = (
        context.progress.last_snapshot
        if context.progress is not None
        and context.progress.last_snapshot is not None
        else BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    )
    issue = context.tracker.fetch_issue(int(state["issue_number"]))
    try:
        pinned_fingerprint = context.ledger.get_attempt(attempt_id).fingerprint
    except Exception:
        pinned_fingerprint = None
    current_fingerprint = compute_fingerprint(issue.title, issue.body)
    fingerprint_match = (
        pinned_fingerprint is not None and pinned_fingerprint == current_fingerprint
    )
    review_gate_met = context.review_state.review_gate_met
    final_payload = _final_checks_payload(context.ledger, attempt_id)
    if final_payload is not None:
        final_green = bool(final_payload.get("green", False))
        raw_dirty = final_payload.get("dirty_files", [])
        dirty_files = tuple(str(path) for path in raw_dirty) if isinstance(raw_dirty, list) else ()
    else:
        final_green = False
        dirty_files = dirty_working_tree_files(context.workspace)
    base_revision = str(state.get("base_revision") or "")
    base_branch = str(state.get("base_branch") or "")
    base_head = _publication_gate_base_head(
        remote_url=context.remote_url,
        base_branch=base_branch,
        redact=context.review_redact,
    )
    verdict = evaluate_publication_gate(
        is_verified_completion=True,
        fingerprint_match=fingerprint_match,
        review_gate_met=review_gate_met,
        final_checks_green=final_green,
        dirty_files=dirty_files,
        base_revision=base_revision,
        base_head=base_head,
    )
    context.ledger.record(
        attempt_id,
        PUBLICATION_GATE_KIND,
        budget,
        detail=(
            f"publication gate: {'ready' if verdict.ready else verdict.reason} "
            f"(unmet={list(verdict.unmet)!r}, base_moved={verdict.base_moved})"
        ),
        payload={
            "ready": verdict.ready,
            "reason": verdict.reason,
            "unmet": list(verdict.unmet),
            "fingerprint_match": fingerprint_match,
            "review_gate_met": review_gate_met,
            "review_status": context.review_state.status,
            "final_checks_green": final_green,
            "dirty_files": list(dirty_files),
            "base_revision": base_revision,
            "base_head": base_head,
            "base_moved": verdict.base_moved,
            "issue_title": issue.title,
        },
    )
    if verdict.ready:
        return {}
    return {"outcome": FAILED, "reason": verdict.reason}


def _state_claim_lists(state: AttemptState) -> dict[str, list[str] | None]:
    """The accepted claim held in graph state, as lists (contract §9, §15).

    ``run_step`` records the normalised accepted claim here so the tail
    renders per-criterion evidence from what the Attempt actually claimed —
    never by re-deriving it. Missing where no claim was accepted.
    """
    raw = state.get("claim")
    if not isinstance(raw, dict):
        return {}
    claim: dict[str, list[str] | None] = {}
    for criterion_id, covering in raw.items():
        if covering is None:
            claim[str(criterion_id)] = None
        elif isinstance(covering, list):
            claim[str(criterion_id)] = [str(item) for item in covering]
    return claim


def delivery_intent(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12 + ADR 0003: record ``delivered`` as the Delivery Intent (§12, §14).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (for a ready
    verdict ``delivery_intent`` records ``delivered`` as a Delivery Intent
    before any delivery write: an outcome row carrying the Attempt id, branch,
    head sha, verdict and the writes the outcome calls for; writing it is the
    point of no return), §14 (``verified completion`` with every Publication
    Gate condition met ends ``delivered``; the rendered comment and
    pull-request bodies and the list of writes are recorded before any write)
    and §15 (the pull-request and comment contents, rendered once here so a
    retried or recovered write sends the same bytes).

    MVP row ids: P-DEL-1, P-DEL-5, P-DEL-7, C-PUB-1, G-TDD-1, L3-DEL-5, L2-17,
    L2-18, L3-REV-10.

    The ``outcomes`` row is recorded here with both rendered bodies and the
    writes, so for ``delivered`` it exists before ``publish`` performs
    anything. ``terminal_writes`` re-records the same outcome idempotently
    and reuses these bytes. Non-ready states pass through untouched.
    """
    if state.get("outcome") != DELIVERED:
        return {}
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    issue_number = int(state["issue_number"])
    attempt_number = int(state["attempt_number"])
    issue_title = str(state.get("issue_title") or "")
    branch = attempt_branch_name(issue_number, attempt_number, issue_title)
    try:
        head_sha = head_revision(context.workspace)
    except GitFailure:
        head_sha = str(state.get("base_revision") or "unknown")
    try:
        stored = context.ledger.get_outcome(attempt_id)
    except Exception:
        stored = None
    _ = stored
    budget = (
        context.progress.last_snapshot
        if context.progress is not None
        and context.progress.last_snapshot is not None
        else BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    )
    workflow_id, digest = _workflow_pin(
        context.ledger, attempt_id, context.effective_configuration
    )
    final_payload = _final_checks_payload(context.ledger, attempt_id)
    validation_commands: list[dict[str, object]] = []
    working_area = "."
    profile_changed: bool | None = None
    if final_payload is not None:
        raw_commands = final_payload.get("commands", [])
        if isinstance(raw_commands, list):
            validation_commands = [dict(entry) for entry in raw_commands if isinstance(entry, dict)]
        raw_area = final_payload.get("working_area")
        if isinstance(raw_area, str):
            working_area = raw_area
        raw_changed = final_payload.get("profile_changed_in_candidate")
        if isinstance(raw_changed, bool):
            profile_changed = raw_changed
    acceptance_criteria = state.get("acceptance_criteria")
    criteria_list: list[dict[str, str]] = (
        [dict(item) for item in acceptance_criteria]
        if isinstance(acceptance_criteria, list)
        else []
    )
    state_claim = _state_claim_lists(state)
    claimed: dict[str, list[str]] = {}
    results: dict[str, str] = {}
    ever_failed: dict[str, bool] = {}
    for item in criteria_list:
        criterion_id = str(item.get("id", ""))
        if not criterion_id:
            continue
        covering = state_claim.get(criterion_id)
        if covering is None:
            claimed[criterion_id] = []
            results[criterion_id] = "uncovered"
            ever_failed[criterion_id] = False
            continue
        claimed[criterion_id] = list(covering)
        criterion_passing = (
            all(
                context.criterion_evidence.latest_result(test_id) is True
                for test_id in covering
            )
            if covering
            else False
        )
        results[criterion_id] = "pass" if criterion_passing else "unknown"
        ever_failed[criterion_id] = any(
            context.criterion_evidence.ever_failed(test_id) for test_id in covering
        )
    disclosure = {
        "status": context.review_state.status,
        "invocations": context.review_state.invocations,
        "review_gate_met": context.review_state.review_gate_met,
    }
    review_reports: list[dict[str, object]] = []
    latest = context.review_state.latest_completed_review
    if latest is not None:
        review_reports.append(
            {
                "ordinal": latest.ordinal,
                "standards_report": latest.standards_findings,
                "spec_report": latest.spec_findings,
            }
        )
    base_revision = str(state.get("base_revision") or "")
    base_head, base_moved = _delivery_base_disclosure(
        context.ledger,
        attempt_id,
        remote_url=context.remote_url,
        base_branch=str(state.get("base_branch") or ""),
        base_revision=base_revision,
        redact=context.review_redact,
    )
    pr_body = render_pull_request_body(
        attempt_id=attempt_id,
        issue_number=issue_number,
        workflow_id=workflow_id,
        configuration_digest=digest,
        base_revision=base_revision,
        base_head=base_head,
        base_moved=base_moved,
        acceptance_criteria=criteria_list,
        claimed_tests=claimed,
        criterion_results=results,
        ever_failed=ever_failed,
        validation_commands=validation_commands,
        review_status=str(disclosure["status"]),
        review_invocations=int(cast(int, disclosure["invocations"])),
        review_reports=review_reports,
        seam_set=list(state.get("seam_set") or []),
        working_area=working_area,
        profile_changed=profile_changed,
    )
    comment_body = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=DELIVERED,
        reason=VERIFIED_COMPLETION_REASON,
        pull_request_url=None,
        branch_name=branch,
        workflow_id=workflow_id,
        configuration_digest=digest,
        unmet_conditions=(),
        next_action=None,
    )
    writes = (
        PUBLISH_WRITE_KIND,
        *terminal_writes_for_outcome(outcome=DELIVERED),
    )
    context.ledger.record(
        attempt_id,
        DELIVERY_INTENT_KIND,
        budget,
        detail=f"delivery intent: delivered {branch}@{head_sha}",
        payload={
            "attempt_id": attempt_id,
            "branch": branch,
            "head_sha": head_sha,
            "verdict": VERIFIED_COMPLETION_REASON,
            "writes": list(writes),
            "pull_request_body": pr_body,
            "terminal_comment_body": comment_body,
        },
    )
    context.ledger.record_terminal_outcome(
        attempt_id,
        outcome=DELIVERED,
        reason=VERIFIED_COMPLETION_REASON,
        pull_request_body=pr_body,
        terminal_comment_body=comment_body,
        writes=writes,
    )
    return {}


def publish(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§13: open the pull request once, keyed by ``(attempt_id, write_kind)``.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (``publish``
    opens the pull request only for a ready verdict: it is never a draft,
    its title is the Target Issue's title, its body is the recorded §15
    body carrying ``Closes #N`` and the Attempt Marker, opened from the
    Attempt's branch against the configured base branch), §13 (the write
    sequence: record the intent, look the effect up remotely by head branch
    plus Attempt Marker, write only if absent, record the receipt; a
    Delivery Intent recorded before this node is the point of no return; a
    pull request from the same branch whose body carries no marker is not
    ours — the lookup never adopts it, L3-DEL-21; a pull request from this
    branch that a human closed is a withdrawal, not an absence — the Attempt
    stops cooperatively and ends ``abandoned``, and the pull request is
    never reopened, L3-DEL-14), §14 (the rendered pull-request body was
    recorded in ``outcomes`` by ``delivery_intent`` before this node runs,
    so a retried write sends the same bytes; the terminal comment linking
    the opened pull request is recorded before the terminal writes perform
    anything) and §15 (the body carries the Attempt Marker
    ``kind=delivery``).

    MVP row ids: P-DEL-1, P-DEL-5, P-DEL-7, P-DEL-10, L3-DEL-5, L3-DEL-14,
    L3-DEL-21, G-TDD-1, L3-REV-10, L2-17, L2-18.

    The intent and receipt rows in ``effects`` are the durable record of the
    write. Non-ready states record nothing and open no pull request.
    """
    if state.get("outcome") != DELIVERED:
        return {}
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    issue_number = int(state["issue_number"])
    attempt_number = int(state["attempt_number"])
    issue_title = str(state.get("issue_title") or "")
    branch = attempt_branch_name(issue_number, attempt_number, issue_title)
    try:
        head_sha = head_revision(context.workspace)
    except GitFailure:
        head_sha = str(state.get("base_revision") or "unknown")
    base_branch = str(state.get("base_branch") or "main")
    workflow_id, digest = _workflow_pin(
        context.ledger, attempt_id, context.effective_configuration
    )
    existing = context.ledger.get_effect(attempt_id, PUBLISH_WRITE_KIND)
    if existing is not None and existing.status == "receipt":
        stored = context.ledger.get_outcome(attempt_id)
        if (
            stored is not None
            and stored.outcome == ABANDONED
            and stored.reason == WITHDRAWAL_REASON
        ):
            return {"outcome": ABANDONED, "reason": WITHDRAWAL_REASON}
        if stored is not None and stored.outcome == DELIVERED:
            _fill_delivered_comment_with_receipt(
                context, attempt_id, branch, workflow_id, digest, existing, stored
            )
        return {}
    recorded = context.ledger.get_outcome(attempt_id)
    pr_body = recorded.pull_request_body if recorded is not None else ""
    payload: dict[str, object] = {
        "attempt_id": attempt_id,
        "branch": branch,
        "head_sha": head_sha,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "verdict": VERIFIED_COMPLETION_REASON,
        "pull_request_body": pr_body,
    }
    context.ledger.record_effect_intent(
        attempt_id, PUBLISH_WRITE_KIND, remote_id=head_sha, payload=payload
    )
    marker = attempt_marker(attempt_id, kind=DELIVERY_COMMENT_KIND)
    found = context.tracker.find_pull_request(branch, marker)
    if found is not None and found.state == "open":
        receipt_payload: dict[str, object] = {
            **payload,
            "pull_request_number": found.number,
            "pull_request_url": found.url,
        }
        context.ledger.record_effect_receipt(
            attempt_id,
            PUBLISH_WRITE_KIND,
            remote_id=str(found.number),
            payload=receipt_payload,
        )
        _record_delivered_comment(
            context,
            attempt_id,
            branch,
            pr_body,
            workflow_id,
            digest,
            pull_request_url=found.url or None,
        )
        return {}
    if found is not None:
        receipt_payload = {
            **payload,
            "pull_request_number": found.number,
            "pull_request_url": found.url,
            "withdrawal": True,
        }
        detail = (
            f"the pull request #{found.number} from this branch was closed "
            "by a human before its receipt (withdrawal); it will not be "
            "reopened."
        )
        comment_body = render_terminal_comment(
            attempt_id=attempt_id,
            outcome=ABANDONED,
            reason=WITHDRAWAL_REASON,
            pull_request_url=found.url or None,
            branch_name=branch,
            workflow_id=workflow_id,
            configuration_digest=digest,
            unmet_conditions=(),
            next_action=terminal_next_action(WITHDRAWAL_REASON),
            detail=detail,
        )
        context.ledger.update_outcome_for_publish(
            attempt_id,
            outcome=ABANDONED,
            reason=WITHDRAWAL_REASON,
            pull_request_body=pr_body,
            terminal_comment_body=comment_body,
            writes=(
                PUBLISH_WRITE_KIND,
                *terminal_writes_for_outcome(outcome=ABANDONED),
            ),
        )
        context.ledger.record_effect_receipt(
            attempt_id,
            PUBLISH_WRITE_KIND,
            remote_id=str(found.number),
            payload=receipt_payload,
        )
        return {"outcome": ABANDONED, "reason": WITHDRAWAL_REASON}
    created = context.tracker.open_pull_request(
        title=issue_title,
        head_branch=branch,
        base_branch=base_branch,
        body=pr_body,
    )
    context.ledger.record_effect_receipt(
        attempt_id,
        PUBLISH_WRITE_KIND,
        remote_id=str(created.number),
        payload={
            **payload,
            "pull_request_number": created.number,
            "pull_request_url": created.url,
        },
    )
    _record_delivered_comment(
        context,
        attempt_id,
        branch,
        pr_body,
        workflow_id,
        digest,
        pull_request_url=created.url or None,
    )
    return {}


def _record_delivered_comment(
    context: AttemptContext,
    attempt_id: str,
    branch: str,
    pr_body: str,
    workflow_id: str,
    digest: str,
    *,
    pull_request_url: str | None,
) -> None:
    """Record the ``delivered`` terminal comment linking the pull request (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (the terminal
    comment links the pull request) and §14 ("Recording before publishing":
    the linking bytes are recorded before the terminal writes perform
    anything, and a retried or recovered write sends the same bytes).

    MVP row ids: P-DEL-1.
    """
    stored = context.ledger.get_outcome(attempt_id)
    if stored is None or stored.outcome != DELIVERED:
        return
    if pull_request_url is not None and pull_request_url in stored.terminal_comment_body:
        return
    comment_body = render_terminal_comment(
        attempt_id=attempt_id,
        outcome=DELIVERED,
        reason=VERIFIED_COMPLETION_REASON,
        pull_request_url=pull_request_url,
        branch_name=branch,
        workflow_id=workflow_id,
        configuration_digest=digest,
        unmet_conditions=(),
        next_action=None,
    )
    context.ledger.update_outcome_for_publish(
        attempt_id,
        outcome=stored.outcome,
        reason=stored.reason,
        pull_request_body=pr_body,
        terminal_comment_body=comment_body,
        writes=stored.writes
        or (PUBLISH_WRITE_KIND, *terminal_writes_for_outcome(outcome=DELIVERED)),
    )


def _fill_delivered_comment_with_receipt(
    context: AttemptContext,
    attempt_id: str,
    branch: str,
    workflow_id: str,
    digest: str,
    existing: object,
    stored: object,
) -> None:
    """Replay the ``delivered`` comment fill after a receipt without a link (§13).

    A crash between the receipt and the comment update re-enters ``publish``
    with the receipt present: the remote pull request is found through the
    receipt's recorded URL, so the linking bytes are still recorded before
    the terminal writes perform anything, and no second pull request opens.
    """
    from coding_agent.ledger.store import EffectRecord, OutcomeRecord

    assert isinstance(existing, EffectRecord)
    assert isinstance(stored, OutcomeRecord)
    raw_url = existing.payload.get("pull_request_url")
    url = raw_url if isinstance(raw_url, str) and raw_url else None
    _record_delivered_comment(
        context,
        attempt_id,
        branch,
        stored.pull_request_body,
        workflow_id,
        digest,
        pull_request_url=url,
    )


def _terminal_unmet_conditions(
    ledger: RunLedger, attempt_id: str
) -> tuple[str, ...]:
    """Every unmet Publication Gate condition for the terminal comment (§12, §15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (the first unmet
    condition names the reason and every unmet condition is recorded for the
    terminal comment) and §15 (the comment carries the unmet conditions).

    MVP row ids: P-DEL-2, P-DEL-6, P-DEL-8, P-DEL-9, C-PUB-2.
    """
    gate_records = [
        record for record in ledger.records(attempt_id) if record.kind == PUBLICATION_GATE_KIND
    ]
    if not gate_records:
        return ()
    raw_unmet = gate_records[-1].payload.get("unmet", [])
    if isinstance(raw_unmet, list):
        return tuple(str(item) for item in raw_unmet)
    return ()


def _terminal_baseline_detail(ledger: RunLedger, attempt_id: str) -> str | None:
    """The reason-specific baseline detail for a ``red-baseline`` comment.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (the terminal
    comment carries the failing baseline commands as its reason-specific
    detail).

    Names the failing baseline commands from the latest ``baseline``
    Validation Evidence row, with the individual failure identifiers where
    evidence provides them — the same facts the ``red-baseline`` reason
    string carries, rendered for a human reading the comment.
    """
    baseline_records = [
        record for record in ledger.records(attempt_id) if record.kind == "baseline"
    ]
    if not baseline_records:
        return None
    return baseline_detail_for_comment(baseline_records[-1].payload)


def _terminal_snapshot_block(
    state: AttemptState, context: AttemptContext, branch: str | None
) -> str | None:
    """The deterministic terminal Continuation Snapshot block (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (when an Attempt
    ends without a pull request while the process is alive, the runtime
    composes a terminal snapshot deterministically, with no model call, from
    the runtime-known facts). Where composition fails there is no snapshot
    and the comment carries none.

    MVP row ids: P-DEL-1, C-OUT-1.
    """
    if branch is None:
        return None
    base_revision = str(state.get("base_revision") or "unknown")
    try:
        commit = head_revision(context.workspace)
    except GitFailure:
        commit = base_revision
    composed = compose_terminal_snapshot(
        attempt_id=str(state["attempt_id"]),
        base_revision=base_revision,
        branch=branch,
        commit=commit,
        changed_files=_handoff_changed_files(context.workspace),
        commands=_handoff_commands(context.criterion_evidence),
        validation_evidence=_handoff_validation_evidence(context.criterion_evidence),
        redactions=dict(context.redactions or {}),
    )
    if composed is None:
        return None
    snapshot_json, snapshot_digest, snapshot_version = composed
    return render_snapshot_block(
        snapshot_json=snapshot_json,
        digest=snapshot_digest,
        version=snapshot_version,
    )


def _assigned_by_us(ledger: RunLedger, attempt_id: str) -> bool:
    """Whether the ledger records the Agent Identity assignment as ours (§5).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Run Ledger
    decides whose assignment it is) and §14 (the Worker's own assignment is
    removed last, only when ``assigned_by_us``). An assignment with no open
    Attempt — or one the ledger does not record as ours — is a human's: it
    keeps the issue ineligible, and ``terminal_writes`` never removes it.

    MVP row ids: C-OUT-1.
    """
    return ledger.get_attempt(attempt_id).assigned_by_us is True


def _limit_crossed_detail(reason: str, budget: BudgetSnapshot) -> str:
    """The reason-specific detail for a ``failed-limit`` comment (§15).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §15 (the terminal
    comment carries the limit crossed) and §11 (the flush lands as a readable
    record, per-bucket tokens and dollars, so the comment reports the spend
    directly).

    MVP row ids: P-LIM-1.

    Names the crossed limit and the flushed spend: cumulative cost,
    effective tokens and tool calls with the wall-clock elapsed. The
    per-bucket dollars ride in the ``usage`` ledger record this finalization
    flushed first.
    """
    usage = budget.usage
    limit = reason
    prefix = "failed-limit:"
    if reason.startswith(prefix):
        limit = reason[len(prefix):].strip() or reason
    return (
        f"limit crossed: {limit}; spend ${usage.cost_usd:.4f}, "
        f"{usage.effective_tokens} effective tokens "
        f"({usage.tokens} total), {usage.tool_calls} tool calls "
        f"in {budget.elapsed_seconds:.0f}s"
    )


def terminal_writes(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§9 + §14 + §15: record the Terminal Outcome first, then perform the writes.

    "Recording before publishing" is an ordering inside this node: the
    ``outcomes`` row — carrying the rendered comment and pull-request bodies
    and the list of writes — exists before the first tracker call, so a
    retried or recovered write sends the same bytes. For ``delivered`` the
    row is already there: ``delivery_intent`` recorded it with both bodies
    and this node reuses those bytes idempotently. For every other outcome
    this node renders the terminal comment (with its reason-specific detail
    and, the Attempt ending without a pull request, the deterministic
    Continuation Snapshot composed with no model call) and records the row
    before performing anything. Over the fake tracker the writes perform
    nothing remotely; the fake records them so tests can assert the order.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (exactly one
    Implementation Outcome is produced, and only ``verified completion``
    may use PASS wording). The ``delivered`` / ``verified-completion``
    comment carries the PASS token; every other outcome's comment carries
    no PASS wording. §14 (the Terminal Outcome follows the Implementation
    Outcome → Terminal Outcome table; the writes run in order: the comment,
    the Selection Label removal, ``ready-for-human`` on every outcome
    without a pull request, then the Worker's own assignment removal) and
    §15 (one terminal comment with the Attempt Marker ``kind=terminal``:
    the outcome and reason, a link to the pull request or the pushed branch,
    the Attempt/Workflow/digest pinning, what a human must do next, and
    every unmet Publication Gate condition).

    The review disclosure (§10, §15) is recorded first of all: the review
    status, the invocation count and every Review Report in full, with the
    last Attempt-wide budget snapshot. Findings never block, so nothing
    here reads them to decide the outcome.

    MVP row ids: L3-IMP-10, L3-IMP-20, P-DEL-1, P-DEL-2, P-DEL-5, P-DEL-6,
    P-DEL-8, P-DEL-9, C-PUB-2, C-OUT-1, C-TRK-1, C-LIM-1, P-LIM-1.
    """
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    outcome = state.get("outcome") or FAILED
    reason = state.get("reason") or "unknown"
    budget = (
        context.progress.last_snapshot if context.progress is not None else None
    ) or BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals())
    disclosure = review_disclosure(context.review_state)
    context.ledger.record(
        attempt_id,
        REVIEW_STATUS_KIND,
        budget,
        detail=(
            f"review status {disclosure['status']} after "
            f"{disclosure['invocations']} invocation(s)"
        ),
        payload=disclosure,
    )
    workflow_id, digest = _workflow_pin(
        context.ledger, attempt_id, context.effective_configuration
    )
    issue_number = int(state["issue_number"])
    attempt_number = int(state["attempt_number"])
    issue_title = str(state.get("issue_title") or "")
    branch: str | None = None
    try:
        branch = attempt_branch_name(issue_number, attempt_number, issue_title)
    except Exception:
        branch = None
    recorded = context.ledger.get_outcome(attempt_id)
    if recorded is None or (
        not recorded.terminal_comment_body
        and not recorded.pull_request_body
        and not recorded.writes
    ):
        if outcome == DELIVERED:
            # Defensive only: ``delivery_intent`` is the first recorder for
            # ``delivered`` and always runs before this node, so a recorded
            # row with both bodies is already there. Without it there is no
            # pull-request body to send; record the comment alone.
            comment_body = render_terminal_comment(
                attempt_id=attempt_id,
                outcome=outcome,
                reason=reason,
                pull_request_url=None,
                branch_name=branch,
                workflow_id=workflow_id,
                configuration_digest=digest,
            )
            pull_request_body = ""
        else:
            unmet = _terminal_unmet_conditions(context.ledger, attempt_id)
            detail: str | None = None
            if unmet:
                detail = f"unmet Publication Gate conditions: {', '.join(unmet)}"
            elif reason in (
                DRIFT_REASON,
                REVIEW_MISSING_REASON,
                FAILING_VALIDATION_REASON,
                DIRTY_TREE_REASON,
            ):
                detail = f"unmet Publication Gate condition: {reason}"
            elif reason.startswith("red-baseline"):
                detail = _terminal_baseline_detail(context.ledger, attempt_id)
            elif outcome == FAILED_LIMIT:
                detail = _limit_crossed_detail(reason, budget)
            snapshot_block = _terminal_snapshot_block(state, context, branch)
            comment_body = render_terminal_comment(
                attempt_id=attempt_id,
                outcome=outcome,
                reason=reason,
                pull_request_url=None,
                branch_name=branch,
                workflow_id=workflow_id,
                configuration_digest=digest,
                unmet_conditions=unmet,
                next_action=terminal_next_action(reason),
                detail=detail,
                snapshot_block=snapshot_block,
            )
            pull_request_body = ""
        context.ledger.record_terminal_outcome(
            attempt_id,
            outcome=outcome,
            reason=reason,
            pull_request_body=pull_request_body,
            terminal_comment_body=comment_body,
            writes=terminal_writes_for_outcome(outcome=outcome),
        )
    stored = context.ledger.get_outcome(attempt_id)
    assert stored is not None
    body = stored.terminal_comment_body
    for write_kind in terminal_writes_for_outcome(outcome=outcome):
        if write_kind == TERMINAL_COMMENT_WRITE_KIND:
            context.tracker.post_terminal_comment(issue_number, body)
        elif write_kind == REMOVE_SELECTION_LABEL_WRITE_KIND:
            context.tracker.remove_selection_label(issue_number)
        elif write_kind == ADD_READY_FOR_HUMAN_WRITE_KIND:
            context.tracker.add_ready_for_human(issue_number)
        elif write_kind == RELEASE_ASSIGNMENT_WRITE_KIND:
            if _assigned_by_us(context.ledger, attempt_id):
                context.tracker.release_assignment(issue_number)
        else:
            raise AssertionError(f"unknown terminal write kind: {write_kind!r}")
    return {"outcome": outcome, "reason": reason}


_NODE_IMPLS = {
    "claim": claim,
    "readiness": readiness,
    "baseline": baseline,
    "run_step": run_step,
    "gate": gate,
    "delivery_snapshot": delivery_snapshot,
    "final_checks": final_checks,
    "publication_gate": publication_gate,
    "delivery_intent": delivery_intent,
    "publish": publish,
    "terminal_writes": terminal_writes,
}


def build_attempt_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the fixed Attempt graph: eleven nodes, no merge, no checkpointer."""
    builder = StateGraph(AttemptState)
    for name in ATTEMPT_NODE_ORDER:
        builder.add_node(name, _NODE_IMPLS[name])
    builder.add_edge(START, ATTEMPT_NODE_ORDER[0])
    for previous, following in zip(ATTEMPT_NODE_ORDER, ATTEMPT_NODE_ORDER[1:]):
        builder.add_edge(previous, following)
    builder.add_edge(ATTEMPT_NODE_ORDER[-1], END)
    return builder.compile(checkpointer=None)


def _classify_uncaught_exception(exc: Exception) -> str:
    """Map an uncaught graph exception to its Terminal Outcome reason (§13, §14).

    Contract: ``docs/contract/mvp-runtime-contract.md`` §13 (a 401 is a
    Credential Failure; a permission refusal is non-transient; any other
    GitHub API failure is infrastructure-class ``github``) and §14 (a
    Credential Failure ends ``failed`` / ``credential-failure``; an
    infrastructure-class failure ends ``failed`` / ``infrastructure-failure``).

    MVP row ids: P-WRK-1, P-WRK-2, L3-ERR-3, C-ERR-2.
    """
    if isinstance(exc, CredentialFailure):
        return "credential-failure"
    if isinstance(exc, PermissionRefusal):
        return "permission-refusal"
    if isinstance(exc, RateLimitExhausted):
        return "infrastructure-failure: github rate limit exhausted"
    if isinstance(exc, GitHubFailure):
        return f"infrastructure-failure: github {exc}"
    if isinstance(exc, GitFailure):
        return f"infrastructure-failure: git {exc}"
    # The tracker adapter's own error types predate the §13 classification in
    # ``github.errors``: any tracker failure the graph did not already
    # classify is infrastructure-class ``github`` (P-WRK-2), never silent.
    if isinstance(exc, (GitHubTrackerError, IssueFetchFailed)):
        return f"infrastructure-failure: github {exc}"
    return "unclassified-error"


def run_attempt(
    *,
    issue_number: int,
    attempt_number: int,
    tracker: TrackerPort,
    ledger: RunLedger,
    model: InvokableToolModel,
    workspace: Path,
    effective_configuration: Mapping[str, object],
    skills_dir: Path,
    toolchain_matrix: SupportedToolchainMatrix | None = None,
    service_env: Mapping[str, str] | None = None,
    service_prober: ServiceProber | None = None,
    command_runner: CommandRunner | None = None,
    evidence_dir: Path | None = None,
    result_cap_limit: int = DEFAULT_RESULT_CAP_LIMIT,
    artifact_store: ArtifactStore | None = None,
    context_window: ContextWindowConfig | None = None,
    cache_breakpoints: bool = True,
    ceilings: LoopCeilings | None = None,
    price: TokenPrices | None = None,
    redactions: Mapping[str, str] | None = None,
    steps: Sequence[WorkflowStep] | None = None,
    remote_url: str | None = None,
    agent_identity: Identity | None = None,
    review_redact: str | None = None,
) -> AttemptResult:
    """Invoke the Attempt graph in-process and return its Terminal Outcome.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (readiness
    resolves every Readiness Fact before any model call; ``baseline`` runs
    the pinned Validation Contract at the Base Revision before any model
    call — a bootstrap failure ends ``failed`` / ``infrastructure-failure``
    and a red baseline ends ``failed`` / ``red-baseline``), §8 (the step
    opens with the Pinned Prefix and the forced activation call), §9 (the
    Implementation Outcome follows the claim and Criterion Evidence alone:
    ``verified completion`` ends ``delivered`` / ``verified-completion``,
    no diff with complete passing evidence ends ``failed`` /
    ``already-satisfied``, anything else ends ``failed`` /
    ``criteria-uncovered`` or ``failed`` / ``no-completion-evidence``; only
    ``verified completion`` may use PASS wording) and §14 (the Terminal
    Outcome is recorded before the terminal writes).

    ``redactions`` maps secret names to values redacted from Continuation
    Snapshots and handoff-failure artifacts before the 32 KiB cap is
    enforced (contract §11, ADR 0013); ``None`` means no redaction.

    ``steps`` is the ordered Workflow Step list (contract §8): ``None``
    runs :data:`DEFAULT_STEPS` (the M1 ``implement`` shape);
    ``agent run --config --workflow`` builds it from the Effective
    Configuration's steps, each ``skill`` or ``prompt_file`` entry in order
    (W-STP-7: prompt bytes come only from the operator's files, verbatim).
    A provided list must be non-empty with unique step ids; each
    :class:`WorkflowStep` validates the rest (exactly one of
    ``skill``/``prompt_file``, ``implement`` alone).

    ``remote_url`` and ``agent_identity`` bind the review-snapshot push
    behind ``activate_code_review`` (contract §10, §13, §18): the runtime
    commits the current tree and pushes the Attempt's branch before any
    reviewer starts. Where either is ``None`` the activation stays the
    plain skill body, so earlier rows bind the step tools without a remote.
    ``review_redact`` carries the credential redacted from every
    ``ls-remote`` and push failure (§13, §18).

    MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4, C-RDY-1, C-RDY-2,
    C-RDY-3, C-RDY-4, C-WSP-1, L1-6, L2-1, L2-2, L2-12, L2-13, L2-14,
    L3-CLR-20, L3-IMP-4, L3-IMP-5, L3-IMP-10, L3-IMP-11, L3-IMP-12,
    L3-IMP-13, L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-20, L3-IMP-21,
    L3-IMP-23, L3-IMP-30, L3-IMP-31, C-CTX-1, C-SKL-1,
    C-SKL-2, G-HDR-1, L3-IMP-1, L3-IMP-15, P-CTX-1, W-STP-1, W-STP-2,
    W-STP-4, W-STP-5, G-STP-1, C-STP-1, C-ERR-1, L3-DEL-19, L3-DEL-20,
    L3-ERR-9, L3-ERR-10, P-DEL-4, P-DEL-6, P-DEL-7, P-DEL-8, P-DEL-9,
    C-PUB-1, C-PUB-2, C-REV-1, L3-REV-1, L3-REV-2, G-REV-1,
    G-REV-2, W-STP-6, P-CAN-1, P-CAN-2, P-CAN-3, W-STP-3, G-REV-3,
    REC-SIG-1, REC-SIG-2.

    An unclassified exception anywhere in the graph (L3-ERR-2) ends the
    Attempt ``failed`` — permanent, not transient — and the outcome is still
    recorded in the ledger before this returns.
    """
    attempt_id = f"#{issue_number}/{attempt_number}"
    if steps is None:
        resolved_steps = DEFAULT_STEPS
    else:
        resolved_steps = tuple(steps)
        if not resolved_steps:
            raise ValueError("run_attempt needs at least one Workflow Step")
        seen_ids = [step.step_id for step in resolved_steps]
        if len(set(seen_ids)) != len(seen_ids):
            raise ValueError(
                f"Workflow Step ids must be unique, got {seen_ids!r}"
            )
    context = AttemptContext(
        tracker=tracker,
        ledger=ledger,
        model=model,
        workspace=workspace,
        effective_configuration=effective_configuration,
        toolchain_matrix=toolchain_matrix,
        service_env=service_env,
        service_prober=service_prober
        if service_prober is not None
        else _default_service_prober(),
        command_runner=command_runner,
        evidence_dir=evidence_dir,
        skills_dir=skills_dir,
        result_cap_limit=result_cap_limit,
        artifact_store=artifact_store,
        context_window=context_window,
        cache_breakpoints=cache_breakpoints,
        ceilings=ceilings if ceilings is not None else LoopCeilings(),
        price=price,
        redactions=redactions,
        steps=resolved_steps,
        remote_url=remote_url,
        agent_identity=agent_identity,
        review_redact=review_redact,
        progress=StepProgress(
            attempt_id,
            ceilings if ceilings is not None else LoopCeilings(),
            ledger=ledger,
        ),
    )
    graph = build_attempt_graph()
    try:
        final = graph.invoke(
            {
                "attempt_id": attempt_id,
                "issue_number": issue_number,
                "attempt_number": attempt_number,
            },
            config={"configurable": context.to_configurable()},
        )
    except Exception as exc:
        try:
            ledger.open_attempt(
                attempt_id, effective_configuration=effective_configuration
            )
        except sqlite3.IntegrityError:
            pass
        reason = _classify_uncaught_exception(exc)
        recorded = ledger.record_terminal_outcome(
            attempt_id, outcome=FAILED, reason=reason
        )
        return AttemptResult(
            attempt_id=attempt_id, outcome=recorded.outcome, reason=recorded.reason
        )
    assert isinstance(final, dict)
    outcome = str(final.get("outcome") or FAILED)
    reason = str(final.get("reason") or "unknown")
    return AttemptResult(attempt_id=attempt_id, outcome=outcome, reason=reason)
