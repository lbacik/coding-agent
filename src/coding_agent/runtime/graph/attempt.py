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
Outcome before it performs anything) and §17 (the ledger tables).

MVP row ids: C-GRF-1, C-SKL-1, C-SKL-2, G-HDR-1, L1-6, L3-SEL-11,
L3-CLR-19, L3-CLR-20, L3-IMP-1, L3-IMP-5, L3-IMP-9, L3-IMP-10, L3-IMP-11,
L3-IMP-13, L3-IMP-15, L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-20, L3-IMP-24,
L3-IMP-25, L3-IMP-26, L3-IMP-30, L3-ERR-2,
P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4,
C-RDY-1, C-RDY-2, C-RDY-3, C-RDY-4, C-WSP-1, L2-1, L2-2, L2-12, L2-13, L2-14,
P-CTX-1, C-IMP-1, L3-IMP-28, L3-IMP-29.

Every node whose behaviour belongs to a later ticket or slice
(``gate``, ``delivery_snapshot``, ``final_checks``,
``publication_gate``, ``delivery_intent``, ``publish``) is a pass-through
until that slice fills it; the graph's shape never changes. ``readiness``
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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from coding_agent.identity.commit_identity import attempt_branch_name
from coding_agent.ledger.store import BudgetSnapshot, RunLedger
from coding_agent.runtime.fingerprint import compute_fingerprint
from coding_agent.profile.parser import parse_profile_yaml
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber, SocketServiceProber
from coding_agent.profile.toolchain import SupportedToolchainMatrix
from coding_agent.runtime.baseline import (
    baseline_evidence_payload,
    baseline_outcome,
    run_baseline,
)
from coding_agent.runtime.ceilings import LoopCeilings, UsageTotals
from coding_agent.runtime.git import read_file_at_revision
from coding_agent.runtime.readiness import PROFILE_RELPATH
from coding_agent.validate.runner import CommandRunner, SubprocessCommandRunner
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.runtime.completion import (
    ClaimRecord,
    decide_implementation_outcome,
    normalize_claim,
)
from coding_agent.runtime.graph.context import AttemptContext
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.step_progress import StepProgress
from coding_agent.runtime.graph.step import (
    FORCED_ACTIVATION_TOOL,
    check_opening_fits_context,
    has_report_completion,
    report_completion_args,
    run_implement_step,
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
    """
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    issue = context.tracker.fetch_issue(state["issue_number"])
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


def _workspace_has_diff(workspace: Path) -> bool:
    """Whether the working tree differs from its Base Revision commit.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (``verified
    completion`` needs a candidate diff; an empty diff without a complete
    per-criterion justification is ``completion not established``, never
    ``already satisfied``) and L3-IMP-10 (the tree identical to the Base
    Revision). ``git status --porcelain`` on a clean checkout is empty, so
    any output — tracked edits or untracked files — is the candidate diff.
    An unreadable tree state never reports a clean tree: it assumes a
    candidate diff so an unknown state cannot become ``already satisfied``.
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
        return bool(result.stdout.strip())
    except Exception:
        return True


def run_step(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§8 + §9 + §11: run the single ``implement`` step, then decide its outcome.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (Opening a step,
    The Attempt Header, Tools; Exit — silence is answered, not obeyed: a
    response with no tool calls and no accepted exit draws one deterministic
    continuation composed from the unmet obligations, budget two per work
    node, monotonic and never reset by progress; once spent the node ends
    ``completion not established`` / ``undeclared``), §9 (the runtime owns
    the evidence: ``test_targeted`` is the sole writer of the per-Attempt
    evidence dir and records Criterion Evidence; ``report_completion`` is
    the only non-exceptional exit with a per-Attempt schema; an unbacked
    claim is returned naming every failing entry, never a progress event;
    after two returns the third call is force-accepted; an explicit
    ``uncovered`` is accepted as ``criteria-uncovered``; exactly one
    Implementation Outcome is decided from the claim and Criterion Evidence
    alone; only ``verified completion`` may use PASS wording) and §11 (an
    oversized tool result is capped with a slice pointer, activation results
    exempt; the assistant turn is carried back verbatim; cache breakpoints
    are applied; a Pinned Prefix that cannot fit with its overhead refuses
    the Attempt before any model work; Progress Gates and the verification
    reserve are Attempt-wide meters observed after every model response and
    every tool result; five consecutive responses without a progress event
    record one soft stall; at 70% of any meter the ledger records one
    durable Gate and at 80% one reserve entry; the phase only moves forward;
    a ceiling stops the loop inside the node; the category follows the
    moment of acceptance, not the moment of measurement), and ADR 0014.
    MVP row ids: C-SKL-1,
    C-SKL-2, G-HDR-1, L2-2, L3-IMP-1, L3-IMP-5, L3-IMP-10, L3-IMP-13,
    L3-IMP-15, L3-IMP-16, L3-IMP-17, L3-IMP-18, L3-IMP-20, L3-IMP-24,
    L3-IMP-25, L3-IMP-26, L3-IMP-30, P-CTX-1, C-IMP-1,
    L3-IMP-28, L3-IMP-29.

    A Readiness refusal recorded above passes straight through with no
    model call. Entering with no Seam Set in state is refused as an
    internal invariant violation (L3-IMP-11): ``failed``, non-transient,
    with no model call.

    The step opens with the Pinned Prefix — the Attempt Header the Worker
    authors, then the Target Issue's title and body (never comments) — and
    the first request forces the ``activate_implement`` call through
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
    runtime-owned ``test_targeted`` tool and the per-Attempt
    ``report_completion`` tool whose schema lists exactly the Attempt's
    ``AC-n`` ids. The step loop returns unbacked claims inside the work
    node (the claim-rejection budget lives here and is restored on a fresh
    node). After the step, the claim is read from the messages and the
    Implementation Outcome is decided from that claim, the force-accept flag
    and the runtime-owned Criterion Evidence alone: ``verified completion``
    (a diff plus passing evidence for every criterion) ends ``delivered`` /
    ``verified-completion``; a force-accepted claim with a diff ends
    ``failed`` / ``unverified`` (``implemented but unverified``); a
    force-accepted claim without one ends ``failed`` /
    ``claims-unverified``; no diff with complete passing evidence ends
    ``failed`` / ``already-satisfied`` (L3-IMP-10); a turn with no tool
    calls never yields an outcome on its own — after two deterministic
    continuations the node ends ``completion not established`` /
    ``undeclared`` whether or not a diff exists (L3-IMP-24); anything else
    is ``completion not established`` (``failed`` / ``criteria-uncovered``
    or ``failed`` / ``no-completion-evidence``, L3-IMP-20).
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
    target_language = str(context.effective_configuration.get("target_language", "python"))
    header = compose_mvp_attempt_header(
        attempt_id=str(state["attempt_id"]),
        base_revision=base_revision,
        branch=branch,
        acceptance_criteria=acceptance_criteria,
        seam_set=seam_set,
        target_language=target_language,
    )
    opening = build_step_opening_messages(header, issue_title, issue_body)
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
    claim_sink: dict[str, object] = {}
    claim_record = ClaimRecord()
    tools = build_implement_step_tools(
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
    )
    if context.context_window is not None:
        refusal = check_opening_fits_context(
            list(opening), list(tools), context.context_window
        )
        if refusal is not None:
            return {"outcome": FAILED, "reason": f"handoff-failure: {refusal}"}
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
            has_diff=_workspace_has_diff(context.workspace),
            executed_tests=executed,
        )

    progress = StepProgress(
        str(state["attempt_id"]),
        context.ceilings,
        ledger=context.ledger,
    )
    started_at = time.monotonic()
    messages = run_implement_step(
        context.model,
        tools,
        opening,
        forced_tool=FORCED_ACTIVATION_TOOL,
        result_cap_limit=context.result_cap_limit,
        artifact_store=store,
        cache_breakpoints=context.cache_breakpoints,
        max_silence_continuations=MAX_SILENCE_CONTINUATIONS,
        silence_message_for_sequence=_silence_text,
        claim_record=claim_record,
        progress=progress,
        ceilings=context.ceilings,
        workspace=context.workspace,
        criterion_evidence=context.criterion_evidence,
        started_at=started_at,
    )
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
        has_diff = _workspace_has_diff(context.workspace)
        outcome, reason = decide_implementation_outcome(
            accepted_claim,
            context.criterion_evidence,
            has_diff=has_diff,
            force_accepted=claim_record.force_accepted,
        )
        if outcome == "verified completion":
            return {"outcome": DELIVERED, "reason": reason}
        return {"outcome": FAILED, "reason": reason}
    if cutoff:
        # Cut short by a ceiling, the verification reserve or a soft stall
        # with no accepted claim (contract §11, L3-IMP-16, L3-IMP-17,
        # L3-IMP-18, L3-IMP-30): saved partial work with a candidate diff,
        # completion not established / undeclared without one.
        if _workspace_has_diff(context.workspace):
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
    has_diff = _workspace_has_diff(context.workspace)
    outcome, reason = decide_implementation_outcome(
        claim,
        context.criterion_evidence,
        has_diff=has_diff,
        force_accepted=claim_record.force_accepted,
    )
    if outcome == "verified completion":
        return {"outcome": DELIVERED, "reason": reason}
    return {"outcome": FAILED, "reason": reason}


def gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§11: pass-through until the Gates slice fills it (single step: proceed)."""
    _ = (state, config)
    return {}


def delivery_snapshot(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def final_checks(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def publication_gate(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def delivery_intent(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§12: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def publish(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§13: pass-through until the delivery slice fills it."""
    _ = (state, config)
    return {}


def terminal_writes(state: AttemptState, config: RunnableConfig) -> dict[str, object]:
    """§9 + §14: record the Terminal Outcome first, then perform the writes.

    "Recording before publishing" is an ordering inside this node: the
    ``outcomes`` row exists before the first tracker call. Over the fake
    tracker (and the M1 local adapter) the writes perform nothing remotely;
    the fake records them so tests can assert the order.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §9 (exactly one
    Implementation Outcome is produced, and only ``verified completion``
    may use PASS wording). The ``delivered`` / ``verified-completion``
    comment carries the PASS token; every other outcome's comment carries
    no PASS wording.

    MVP row ids: L3-IMP-10, L3-IMP-20.
    """
    context = AttemptContext.from_config(config)
    attempt_id = state["attempt_id"]
    outcome = state.get("outcome") or FAILED
    reason = state.get("reason") or "unknown"
    context.ledger.record_terminal_outcome(attempt_id, outcome=outcome, reason=reason)
    if outcome == DELIVERED and reason == "verified-completion":
        body = f"Attempt {attempt_id}: PASS {outcome} ({reason})"
    else:
        body = f"Attempt {attempt_id}: {outcome} ({reason})"
    context.tracker.post_terminal_comment(state["issue_number"], body)
    context.tracker.remove_selection_label(state["issue_number"])
    if outcome != DELIVERED:
        context.tracker.add_ready_for_human(state["issue_number"])
    context.tracker.release_assignment(state["issue_number"])
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

    MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, P-RDY-4, C-RDY-1, C-RDY-2,
    C-RDY-3, C-RDY-4, C-WSP-1, L1-6, L2-1, L2-2, L2-12, L2-13, L2-14,
    L3-CLR-20, L3-IMP-5, L3-IMP-10, L3-IMP-11, L3-IMP-13, L3-IMP-16,
    L3-IMP-17, L3-IMP-18, L3-IMP-20, L3-IMP-30, C-SKL-1,
    C-SKL-2, G-HDR-1, L3-IMP-1, L3-IMP-15, P-CTX-1.

    An unclassified exception anywhere in the graph (L3-ERR-2) ends the
    Attempt ``failed`` — permanent, not transient — and the outcome is still
    recorded in the ledger before this returns.
    """
    attempt_id = f"#{issue_number}/{attempt_number}"
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
    except Exception:
        try:
            ledger.open_attempt(
                attempt_id, effective_configuration=effective_configuration
            )
        except sqlite3.IntegrityError:
            pass
        recorded = ledger.record_terminal_outcome(
            attempt_id, outcome=FAILED, reason="unclassified-error"
        )
        return AttemptResult(
            attempt_id=attempt_id, outcome=recorded.outcome, reason=recorded.reason
        )
    assert isinstance(final, dict)
    outcome = str(final.get("outcome") or FAILED)
    reason = str(final.get("reason") or "unknown")
    return AttemptResult(attempt_id=attempt_id, outcome=outcome, reason=reason)
