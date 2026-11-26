"""The Run Ledger's SQLite store: events, Attempts, effects and Terminal Outcomes.

Contract: ``docs/contract/mvp-runtime-contract.md`` §10 (the review
snapshot commits the current tree and pushes the branch as X), §13 (the write sequence: pass the in-tool Gate, record
the intent keyed by ``(attempt_id, write_kind)``, look the remote ref up,
push only when it is absent or behind, record the receipt; a kind that can
repeat, the review-snapshot push, carries its ordinal), §17 ("Storage": one
SQLite file on the state volume, opened in WAL mode with
``synchronous=FULL``; tables created with ``CREATE TABLE IF NOT EXISTS`` and
no schema version), §14 ("Recording before publishing": the Terminal
Outcome row exists before ``terminal_writes`` performs any tracker call; the
row carries the outcome, its reason, the rendered comment and pull-request
bodies and the list of writes it calls for, so a retried or recovered write
sends the same bytes), §15 (the pull-request and terminal comment contents)
and §18 (one branch per Attempt, ``agent/<issue>/<n>-<slug>``; author and
committer are the id-based noreply address; every commit carries the
``Attempt: #<issue>/<n>`` trailer) and §5 (the Claim records the Fingerprint
beside the Effective Configuration; L1-6 reads both back).

MVP row ids: REC-OUT-1, REC-OUT-2, C-GRF-1, L1-6, L3-DEL-19, L3-DEL-20,
L3-ERR-9, L3-ERR-10, P-DEL-1, P-DEL-10, L3-DEL-5, L3-DEL-14, L3-DEL-21,
L3-REV-10, P-DEL-5, L2-17, L2-18, C-OUT-1, W-PIN-2, P-CLM-1, P-CLM-2,
REC-CLM-1, REC-CLM-2, REC-CFG-2, REC-WRK-1, C-LIM-1, P-LIM-1, L3-IMP-7,
L3-ERR-11.

Contract §18 (the ledger is an output surface: every detail, reason,
body and payload passes through the redaction registry before it is
stored, so a stored row carries only ``«redacted:<name>»`` markers).
Every ``record_*`` method funnels through the one registry held here;
callers never redact on their own.

Contract §11 (the M7 hard-ceiling finalization): usage is counted after
every model response and flushed to the Run Ledger at that moment as a
readable ``usage`` record carrying cumulative totals plus per-bucket tokens
and dollars; the wall clock is measured from the Attempt's opening in the
Run Ledger, never from process start, so review time counts too.
``USAGE_KIND`` is that record's event kind.

M1 tables are ``run_ledger_events`` (progress, budget and snapshot
references, carried over from ``runtime.progress`` unchanged), ``attempts``
(one row per ``#<issue>/<n>`` with its lifecycle state and its Effective
Configuration) and ``outcomes`` (one row per Attempt with the Terminal
Outcome, its reason, the rendered comment and pull-request bodies and the
writes it calls for). The Effective
Configuration column holds the normalised Effective Configuration record
``agent run`` pins on the Attempt
(``workflow.effective.build_effective_configuration``) and its SHA-256
digest (contract §2.10, CFG-41); scripted tests pass their own settings.
``effects`` arrives in M3, keyed by
``(attempt_id, write_kind)`` with a status of ``intent`` or ``receipt``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from coding_agent.inputs.comments import PinnedComment
    from coding_agent.operator.commands import CommandRecord

from coding_agent.operator import archive as attempt_archives
from coding_agent.operator.commands import (
    complete_pending_stop,
    count_stop_plan,
    ensure_operator_tables,
    get_pending_next_command,
    read_intake,
    reconcile_accepted,
    resolve_matching_next_on_finalize,
    resolve_next_command as _resolve,
)
from coding_agent.operator.contract import to_z, utc_now_z
from coding_agent.operator.events import (
    EVENT_HALTED,
    LEVEL_ERROR,
    MAX_EVENT_TEXT_LEN,
    emit_contract_event,
    ensure_operator_events_table,
)
from coding_agent.operator.runlog import (
    discard_pending_events,
    flush_pending_events,
    print_contract_event,
    reset_pending_events,
)
from coding_agent.operator.store import (
    read_instance_repository,
    read_process_started_at,
)
from coding_agent.runtime.ceilings import LoopCeilings, OriginCell, UsageTotals
from coding_agent.runtime.redaction import Redactable, registry_from

OPEN = "open"
CLAIM_CONFIRMED = "claim-confirmed"
OUTCOME_RECORDED = "outcome-recorded"
FINISHED = "finished"
RELEASED = "released"

AttemptState = Literal["open", "claim-confirmed", "outcome-recorded", "finished", "released"]

PROGRESS_ACCEPTANCE = "acceptance_criterion"
PROGRESS_SEAM = "seam_set"
PROGRESS_READINESS = "readiness"
PROGRESS_DIFF = "diff"
PROGRESS_TARGETED_RESULT = "targeted_result"
PROGRESS_VALIDATION_RESULT = "validation_result"

USAGE_KIND = "usage"
"""Run Ledger event kind for a flushed usage record (contract §11, C-LIM-1).

One row per model response and per tool result while the Attempt runs, plus
one final row before the hard-ceiling finalization. The payload carries the
cumulative totals plus per-bucket tokens and dollars, so the terminal comment
and an operator report the spend directly instead of computing
limit-minus-remaining.
"""


@dataclass(frozen=True)
class BudgetSnapshot:
    """The Attempt-wide meter values at one observable ledger boundary."""

    elapsed_seconds: float
    usage: UsageTotals
    context: dict[str, Any] | None = None
    """The main conversation's context (``used``/``peak``/``estimate``/
    ``conversation``) with its cache counters (``cache_read``/``cache_input``/
    ``responses_without_usage``) for the operator snapshot, if tracked.

    ``None`` where the flusher tracks no context (older call sites): the
    snapshot then reads the context figures ``unavailable``. ``Any`` values
    keep the payload JSON-shaped without importing the budgets layer.
    """
    by_origin: dict[str, OriginCell] | None = None
    """The Attempt spend per Usage Origin (``main``/``subagent``).

    ``None`` where per-origin tracking is disabled — the last flush carried
    no ``by_origin`` (an archived ledger) — so later flushes carry none and
    per-origin figures stay ``unavailable``.
    """

    def remaining(self, ceilings: LoopCeilings) -> dict[str, float | int | None]:
        """Return remaining capacity without inventing a limit for an unset meter."""
        return {
            "wall_clock_seconds": (
                max(0.0, ceilings.max_wall_clock_seconds - self.elapsed_seconds)
                if ceilings.max_wall_clock_seconds is not None
                else None
            ),
            "cost_usd": (
                max(0.0, ceilings.max_cost_usd - self.usage.cost_usd)
                if ceilings.max_cost_usd is not None
                else None
            ),
            "effective_tokens": (
                max(0, ceilings.max_effective_tokens - self.usage.effective_tokens)
                if ceilings.max_effective_tokens is not None
                else None
            ),
            "tool_calls": (
                max(0, ceilings.max_tool_calls - self.usage.tool_calls)
                if ceilings.max_tool_calls is not None
                else None
            ),
        }


def digest_effective_configuration(settings: Mapping[str, object]) -> str:
    """Digest the Effective Configuration bytes the ledger pins per Attempt.

    SHA-256 of the canonical identity core: sorted keys, compact
    separators, ``ensure_ascii=True``, ``allow_nan=False``, no BOM and no
    trailing newline (SKV3-26/27, ``version: 2``).
    """
    from coding_agent.workflow.effective import dumps_effective_canonical

    return hashlib.sha256(dumps_effective_canonical(dict(settings))).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One durable Run Ledger event, including the budget at its boundary."""

    id: int
    attempt_id: str
    kind: str
    detail: str | None
    artifact_ref: str | None
    diff_ref: str | None
    remaining_budget: dict[str, float | int | None]
    payload: dict[str, object]


@dataclass(frozen=True)
class AttemptRecord:
    """One ``attempts`` row: the Attempt's lifecycle state and pinning.

    Contract §5: the Fingerprint recorded at Claim time travels with the
    Effective Configuration (L1-6 reads both back), and ``assigned_by_us``
    records whether the Agent Identity's assignment is the Worker's own —
    only then does ``terminal_writes`` remove it (C-OUT-1). At the Claim
    boundary the ledger also records the Effective Configuration core (the
    stored ``effective_configuration`` itself), its digest and the
    content-reference index: every prompt, activation delivery and delivered
    component digest the Attempt Archive holds, with ``not-delivered``
    digests pinned but unarchived.
    """

    attempt_id: str
    state: str
    effective_configuration: dict[str, object]
    effective_configuration_digest: str
    fingerprint: str | None = None
    base_revision: str | None = None
    agent_identity_id: int | None = None
    assigned_by_us: bool | None = None
    content_refs: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeRecord:
    """One ``outcomes`` row: the recorded Terminal Outcome and its reason.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §14 ("Recording
    before publishing": the row carries the outcome, its reason, the rendered
    comment and pull-request bodies and the list of writes it calls for.
    ``pull_request_body`` is the rendered §15 body for a ready pull request
    and an empty string where the Attempt opens none; ``writes`` lists the
    §14 terminal writes in the order they are performed.

    MVP row ids: REC-OUT-1, REC-OUT-2, P-DEL-1, C-OUT-1.
    """

    attempt_id: str
    outcome: str
    reason: str
    pull_request_body: str = ""
    terminal_comment_body: str = ""
    writes: tuple[str, ...] = ()


LEDGER_SCHEMA_VERSION = 1
"""The explicit Run Ledger schema version (step-workflow contract §13.1).

The MVP wrote ledgers with no version; the first versioned schema is 1.
Every later change is additive: ``RunLedger`` creates missing tables and
columns on open, so a ledger file written by the MVP runtime keeps working
until the S10 cutover (matrix row REC-3). ``ledger_schema`` holds exactly
one row with this version.
"""


def legacy_unfinished_attempts(
    attempts: tuple[AttemptRecord, ...],
) -> tuple[AttemptRecord, ...]:
    """Every unfinished Legacy Attempt in ``attempts`` (§16.4, REC-11).

    Only states outside ``finished``/``released`` refuse startup; finished
    legacy Attempts remain read-only history for numbering and ``attempts
    list``. A stored id that is not an Attempt Id at all is not legacy:
    see :func:`malformed_unfinished_attempts`.
    """
    from coding_agent.attempt_id import AttemptId

    return tuple(
        attempt
        for attempt in attempts
        if attempt.state not in ("finished", "released")
        and (parsed := AttemptId.try_parse(attempt.attempt_id)) is not None
        and parsed.is_legacy
    )


def malformed_unfinished_attempts(
    attempts: tuple[AttemptRecord, ...],
) -> tuple[AttemptRecord, ...]:
    """Every unfinished Attempt in ``attempts`` whose id is not an Attempt Id.

    Like :func:`legacy_unfinished_attempts`, only states outside
    ``finished``/``released`` refuse startup. A malformed row refuses
    startup with its own reason: the ledger holds a row that is not an
    Attempt Id — never the "unfinished MVP Attempt" message.
    """
    from coding_agent.attempt_id import AttemptId

    return tuple(
        attempt
        for attempt in attempts
        if attempt.state not in ("finished", "released")
        and AttemptId.try_parse(attempt.attempt_id) is None
    )

STEP_EXECUTION_OPEN = "open"
STEP_EXECUTION_ENDED = "ended"
STEP_EXECUTION_INTERRUPTED = "interrupted"

StepExecutionState = Literal["open", "ended", "interrupted"]

STEP_TRANSITION_ROUTE = "route"
STEP_TRANSITION_REFUSED = "refused"
STEP_TRANSITION_END = "end"

StepTransitionKind = Literal["route", "refused", "end"]

ADMISSION_HOLD_HELD = "held"
ADMISSION_HOLD_SETTLED = "settled"
ADMISSION_HOLD_CHARGED = "charged"

AdmissionHoldStatus = Literal["held", "settled", "charged"]

STEP_EFFECT_STATUS_INTENT = "intent"
STEP_EFFECT_STATUS_RECEIPT = "receipt"
STEP_EFFECT_STATUS_NOT_PERFORMED = "not-performed"

StepEffectStatus = Literal["intent", "receipt", "not-performed"]


def digest_effect_payload(payload: Mapping[str, object]) -> str:
    """Digest an effect payload's canonical JSON bytes for audit (§13)."""
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StepExecutionRecord:
    """One ``step_executions`` row: a Step Execution's opening and ending.

    Step-workflow contract §13.2 (matrix row REC-1): the opening — step,
    sequence number and entry Route (previous step, Step Result) — is
    written before the step starts; the end — the Step Result with its
    payload, execution facts, usage and any ``limit-exceeded`` fact — is
    committed before the router selects a successor. A Step Execution left
    open by a process loss is closed as ``interrupted`` by recovery: a
    runtime fact, never a Step Result, never routed.
    """

    attempt_id: str
    step_id: str
    seq: int
    entry_from_step: str | None
    entry_result: str | None
    state: str
    result: str | None = None
    payload: dict[str, object] | None = None
    facts: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    limit_fact: dict[str, object] | None = None


@dataclass(frozen=True)
class StepTransitionRecord:
    """One ``step_transitions`` row: the Route taken, a refused entry or a Workflow End.

    Step-workflow contract §13.2 (matrix row REC-2, owned by S3): a refused
    entry (``execution-ceiling``) creates no Step Execution; the End row
    records the Workflow End the Attempt reached.
    """

    id: int
    attempt_id: str
    kind: str
    from_step: str | None
    result: str | None
    to_step: str | None
    to_end: str | None
    reason: str | None = None


@dataclass(frozen=True)
class AdmissionHoldRecord:
    """One ``admission_holds`` row: a worst-case hold and its settlement.

    Step-workflow contract §11.4, §13.2 (matrix rows LIM-7, LIM-8): the
    bound is held while the request is in flight so concurrent Sub-agents
    cannot both pass on the same headroom, and settled with the actual
    usage on response. A hold still open after a crash is charged its
    worst case.
    """

    id: int
    attempt_id: str
    step_id: str
    seq: int
    hold_id: str
    bound_tokens: int
    bound_cost_usd: float
    status: str


@dataclass(frozen=True)
class StepEffectRecord:
    """One ``step_effects`` row: the intent or receipt of one external write.

    Step-workflow contract §14.1 (matrix row REC-4): the key is
    ``(Attempt id, step, Step Execution number, write ordinal)``; Claim and
    finalisation writes, which belong to no step, use the reserved step
    ``""`` with execution number 0. Contents are the exact rendered payload
    and the expected target state, written after the Gate immediately
    before the write; the receipt carries the remote sha, pull-request
    number, review id or comment id. The key travels in the write's
    Attempt Marker.
    """

    attempt_id: str
    step_id: str
    execution_number: int
    ordinal: int
    status: str
    rendered_payload: str
    expected_target: dict[str, object]
    remote_id: str | None = None
    payload_digest: str = ""


def step_effect_key(attempt_id: str, step_id: str, execution_number: int, ordinal: int) -> str:
    """The Effect Intent key text ``(Attempt, step, n, ordinal)`` (§14.1)."""
    return f"{attempt_id}/{step_id or '<claim>'}/{execution_number}/{ordinal}"


def step_effect_marker(
    attempt_id: str, step_id: str, execution_number: int, ordinal: int, *, kind: str
) -> str:
    """The Attempt Marker line carrying an Effect Intent key (§14.1, §14.4).

    Additive beside ``runtime.publication.attempt_marker``: the marker keeps
    the machine-readable Attempt id and write kind, and appends the Effect
    Intent key so restart settlement can look the write up remotely by key.
    """
    return (
        f"<!-- coding-agent attempt={attempt_id} kind={kind} "
        f"step={step_id or '-'} n={execution_number} ordinal={ordinal} -->"
    )


@dataclass(frozen=True)
class AttemptPin:
    """The input state pinned at Claim (step-workflow contract §5.1, §6, §7).

    ``fingerprint`` is the digest of the read-back title and body; the same
    content the model sees. ``base_revision`` is the base-branch tip at Claim
    for an issue input, and the merge base of the pinned head and that tip
    for a pull-request input. ``input_head``, ``head_branch`` and
    ``base_branch`` are set only for pull-request inputs. ``selection_actor``
    (login, numeric id, account type and time) and ``identity_mode`` record
    the authorisation the Claim read-back checked (§5.1, INP-19, INP-20).
    """

    attempt_id: str
    workflow_id: str
    input_type: str
    input_number: int
    title: str
    body: str
    fingerprint: str
    base_revision: str
    base_tip: str | None = None
    input_head: str | None = None
    head_branch: str | None = None
    base_branch: str | None = None
    selection_actor: str | None = None
    selection_actor_id: int | None = None
    selection_actor_type: str | None = None
    selection_actor_at: str | None = None
    identity_mode: str | None = None


@dataclass(frozen=True)
class PinnedCommentSet:
    """The whole-set record of the Trusted Comments pinned at Claim (§7.4).

    ``set_digest`` covers the canonical Archive blob; ``comment_count``
    counts the pinned comments; the three numeric identity ids are the
    selector inputs, kept so the terminal comment can disclose late
    Trusted Comments the Attempt never saw.
    """

    attempt_id: str
    set_digest: str
    comment_count: int
    issue_author_id: int | None = None
    owner_id: int | None = None
    agent_id: int | None = None


@dataclass(frozen=True)
class MergePending:
    """The pending second parent of a pull-request Attempt (§10.8, ADR 0034).

    Recorded by ``update-from-base`` after it merges the pinned Base Tip
    into the workspace without committing: the tip itself, the automatic
    merge result's tree, and the conflicted paths the model may resolve.
    The next ``commit-and-push`` that creates a commit consumes it as the
    merge commit's second parent. Durable in the Run Ledger, so recovery
    can see it.
    """

    attempt_id: str
    base_tip: str
    auto_tree: str
    conflicted_paths: tuple[str, ...]


class RunLedger:
    """One SQLite file holding the Worker's events, Attempts and outcomes.

    Opened in WAL mode with ``synchronous=FULL`` (§17). Every table is
    created with ``CREATE TABLE IF NOT EXISTS`` and there is no schema
    version: re-opening an existing file creates nothing new.
    """

    def __init__(
        self,
        path: Path,
        *,
        ceilings: LoopCeilings | None = None,
        registry: Redactable = None,
    ) -> None:
        """Open the ledger, holding the redaction registry (§18, L3-ERR-11).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §18 (the ledger
        is an output surface: every detail, reason, body and payload passes
        through the redaction registry before it is stored). ``registry``
        accepts any :data:`Redactable` form; callers never redact on their
        own. The default holds no secrets, so scripted tests without one
        stay green.

        MVP row ids: L3-ERR-11.

        Concurrency (contract §8, ADR 0020): concurrent Sub-agents share this
        handle across threads. The connection opens with
        ``check_same_thread=False`` and every method touching it holds the
        one re-entrant writer lock, so ledger writes from concurrent
        Sub-agents serialise instead of interleaving.
        """
        from coding_agent.runtime.redaction import SecretRegistry

        self.path = path
        self.ceilings = ceilings or LoopCeilings()
        self._registry = (
            registry if isinstance(registry, SecretRegistry) else registry_from(registry)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            reset_pending_events(self._connection)
        except Exception:
            pass
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_ledger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT,
                artifact_ref TEXT,
                diff_ref TEXT,
                remaining_budget TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                effective_configuration TEXT NOT NULL,
                effective_configuration_digest TEXT NOT NULL,
                fingerprint TEXT,
                base_revision TEXT,
                agent_identity_id INTEGER,
                assigned_by_us INTEGER,
                responses_without_usage INTEGER NOT NULL DEFAULT 0,
                responses_with_unreported_cache_write INTEGER NOT NULL DEFAULT 0,
                content_refs TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                attempt_id TEXT PRIMARY KEY,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                pull_request_body TEXT NOT NULL DEFAULT '',
                terminal_comment_body TEXT NOT NULL DEFAULT '',
                writes TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_outcomes_columns()
        self._ensure_attempts_columns()
        # The legacy ``effects`` table is retired: new ledgers do not create
        # it, and the runtime never reads or writes it. Ledger files that
        # already carry the table keep it and its rows untouched on open
        # (additive schema evolution, §13.1).
        self._ensure_step_tables()
        self._ensure_ledger_schema()
        ensure_operator_tables(self._connection)
        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_halt (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
        except Exception:
            pass
        try:
            ensure_operator_events_table(self._connection)
        except Exception:
            pass
        try:
            self._connection.commit()
        except Exception:
            pass

    def _ensure_outcomes_columns(self) -> None:
        """Add the M4 body columns to ``outcomes`` rows written before M4.

        Tables are created with ``CREATE TABLE IF NOT EXISTS`` and there is no
        schema version (§17): a ledger file opened by an earlier slice holds an
        ``outcomes`` table with only ``(attempt_id, outcome, reason,
        created_at)``. The new columns carry defaults, so old rows read back
        as an empty pull-request body and no writes.
        """
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(outcomes)").fetchall()
        }
        for column, default in (
            ("pull_request_body", "''"),
            ("terminal_comment_body", "''"),
            ("writes", "'[]'"),
        ):
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE outcomes ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}"
                )
        self._connection.commit()

    def _ensure_attempts_columns(self) -> None:
        """Add later ``attempts`` columns to ledger files written before them.

        Contract: ``docs/contract/mvp-runtime-contract.md`` §5 (the Run
        Ledger decides whose assignment it is) and §17 (no schema version: a
        ledger file opened by an earlier slice holds an ``attempts`` table
        without this column). The ``assigned_by_us`` column is nullable, so
        old rows read back as "not recorded as ours" and the Worker's
        assignment is never removed for them (C-OUT-1). The
        ``responses_without_usage`` counter defaults to zero, so Attempts
        recorded before truthful-usage accounting read back as complete.
        """
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
        if "assigned_by_us" not in existing:
            self._connection.execute("ALTER TABLE attempts ADD COLUMN assigned_by_us INTEGER")
        if "responses_without_usage" not in existing:
            self._connection.execute(
                "ALTER TABLE attempts ADD COLUMN responses_without_usage"
                " INTEGER NOT NULL DEFAULT 0"
            )
        if "responses_with_unreported_cache_write" not in existing:
            self._connection.execute(
                "ALTER TABLE attempts ADD COLUMN responses_with_unreported_cache_write"
                " INTEGER NOT NULL DEFAULT 0"
            )
        if "content_refs" not in existing:
            self._connection.execute(
                "ALTER TABLE attempts ADD COLUMN content_refs TEXT NOT NULL DEFAULT '{}'"
            )
        self._connection.commit()

    @staticmethod
    def _content_index_for(settings: Mapping[str, object]) -> dict[str, object]:
        """The content-reference index for ``settings``, best-effort.

        The ledger records the Effective Configuration core (the stored
        record itself), its digest and this index at the Claim boundary.
        A record that is not a full v2 shape indexes nothing instead of
        refusing: the Claim still opens and recovery still applies the
        pinned-values rules.
        """
        try:
            from coding_agent.workflow.effective import content_index_for_record

            index = content_index_for_record(settings)
            json.dumps(index, sort_keys=True)
            return index
        except Exception:
            return {"prompts": {}, "activations": {}, "components": {}}

    def _ensure_ledger_schema(self) -> None:
        """Stamp the explicit schema version on first versioned open (§13.1).

        Step-workflow contract §13.1 (matrix row REC-3): the ledger carries
        an explicit schema version and its evolution is additive. A ledger
        file written before versioning gains the ``ledger_schema`` table
        holding ``LEDGER_SCHEMA_VERSION`` on open; the MVP tables are
        untouched, so the old runtime keeps working until S10.
        """
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_schema (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            )
            """
        )
        row = self._connection.execute(
            "SELECT version FROM ledger_schema WHERE id = 1"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO ledger_schema (id, version) VALUES (1, ?)",
                (LEDGER_SCHEMA_VERSION,),
            )
        self._connection.commit()

    def _ensure_step_tables(self) -> None:
        """Create the step-workflow recording tables (contract §13.2, §14.1).

        Additive: ``CREATE TABLE IF NOT EXISTS`` beside the MVP tables, so
        a ledger file written by the MVP runtime opens unchanged and the
        old runtime keeps working until S10 (matrix row REC-3).
        """
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS step_executions (
                attempt_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                entry_from_step TEXT,
                entry_result TEXT,
                state TEXT NOT NULL,
                result TEXT,
                payload TEXT,
                facts TEXT,
                usage TEXT,
                limit_fact TEXT,
                opened_at TEXT NOT NULL,
                ended_at TEXT,
                PRIMARY KEY (attempt_id, step_id, seq)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS step_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                from_step TEXT,
                result TEXT,
                to_step TEXT,
                to_end TEXT,
                kind TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admission_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                hold_id TEXT NOT NULL UNIQUE,
                bound_tokens INTEGER NOT NULL,
                bound_cost_usd REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                settled_at TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS step_effects (
                attempt_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                execution_number INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                rendered_payload TEXT NOT NULL,
                expected_target TEXT NOT NULL,
                remote_id TEXT,
                payload_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, step_id, execution_number, ordinal)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempt_pins (
                attempt_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                input_type TEXT NOT NULL,
                input_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                base_revision TEXT NOT NULL,
                base_tip TEXT,
                input_head TEXT,
                head_branch TEXT,
                base_branch TEXT,
                selection_actor TEXT,
                selection_actor_id INTEGER,
                selection_actor_type TEXT,
                selection_actor_at TEXT,
                identity_mode TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attempt_merge_state (
                attempt_id TEXT PRIMARY KEY,
                base_tip TEXT NOT NULL,
                auto_tree TEXT NOT NULL,
                conflicted_paths TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pinned_comment_sets (
                attempt_id TEXT PRIMARY KEY,
                set_digest TEXT NOT NULL,
                comment_count INTEGER NOT NULL,
                issue_author_id INTEGER,
                owner_id INTEGER,
                agent_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pinned_input_comments (
                attempt_id TEXT NOT NULL,
                comment_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                author_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                edited INTEGER NOT NULL,
                body TEXT NOT NULL,
                digest TEXT NOT NULL,
                workflow_id TEXT,
                PRIMARY KEY (attempt_id, comment_id)
            )
            """
        )
        self._connection.commit()

    def schema_version(self) -> int:
        """The explicit Run Ledger schema version (§13.1, REC-3)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT version FROM ledger_schema WHERE id = 1"
            ).fetchone()
        if row is None:
            return LEDGER_SCHEMA_VERSION
        return int(row[0])

    def record_response_without_usage(self, attempt_id: str) -> int:
        """Count one model response that settled at its worst case (§11.4).

        Truthful-usage accounting: every model response without usage
        metadata — or with partial usage — is charged its Admission hold's
        worst case, and increments this Attempt's durable counter, so the
        Status Snapshot reports its token and cost figures as
        ``upper-bound`` with a reason instead of a measured zero. The
        counter lives on the ``attempts`` row, so a Worker restart reads
        back the same count from the ledger file.
        """
        with self._lock:
            try:
                self._connection.execute(
                    "UPDATE attempts SET responses_without_usage = "
                    "COALESCE(responses_without_usage, 0) + 1 "
                    "WHERE attempt_id = ?",
                    (attempt_id,),
                )
                self._connection.commit()
            except sqlite3.OperationalError:
                try:
                    self._connection.execute(
                        "ALTER TABLE attempts ADD COLUMN responses_without_usage"
                        " INTEGER NOT NULL DEFAULT 0"
                    )
                    self._connection.execute(
                        "UPDATE attempts SET responses_without_usage = "
                        "COALESCE(responses_without_usage, 0) + 1 "
                        "WHERE attempt_id = ?",
                        (attempt_id,),
                    )
                    self._connection.commit()
                except sqlite3.Error:
                    try:
                        self._connection.rollback()
                    except sqlite3.Error:
                        pass
            row = self._connection.execute(
                "SELECT responses_without_usage FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def get_responses_without_usage(self, attempt_id: str) -> int:
        """How many model responses for ``attempt_id`` settled at worst case.

        Zero where the Attempt is unknown or predates truthful-usage
        accounting: an old row without the counter reads back as complete,
        never as a failure.
        """
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.get_responses_without_usage(
                self._connection, attempt_id
            )

    def record_response_with_unreported_cache_write(self, attempt_id: str) -> int:
        """Count one gateway response with an unreported cache write (§11.4).

        The response keeps its reported token buckets — effective tokens stay
        true — while its uncached remainder prices at max(input, cache-write).
        The counter is the ledger marker the Status Snapshot reads: any
        positive count reports the Attempt's token and cost figures as
        ``upper-bound``. The counter lives on the ``attempts`` row, so a
        Worker restart reads back the same count from the ledger file.
        """
        with self._lock:
            try:
                self._connection.execute(
                    "UPDATE attempts SET responses_with_unreported_cache_write = "
                    "COALESCE(responses_with_unreported_cache_write, 0) + 1 "
                    "WHERE attempt_id = ?",
                    (attempt_id,),
                )
                self._connection.commit()
            except sqlite3.OperationalError:
                try:
                    self._connection.execute(
                        "ALTER TABLE attempts ADD COLUMN "
                        "responses_with_unreported_cache_write"
                        " INTEGER NOT NULL DEFAULT 0"
                    )
                    self._connection.execute(
                        "UPDATE attempts SET responses_with_unreported_cache_write = "
                        "COALESCE(responses_with_unreported_cache_write, 0) + 1 "
                        "WHERE attempt_id = ?",
                        (attempt_id,),
                    )
                    self._connection.commit()
                except sqlite3.Error:
                    try:
                        self._connection.rollback()
                    except sqlite3.Error:
                        pass
            try:
                row = self._connection.execute(
                    "SELECT responses_with_unreported_cache_write FROM attempts"
                    " WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
            except sqlite3.Error:
                return 0
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def get_responses_with_unreported_cache_write(self, attempt_id: str) -> int:
        """How many gateway responses left their cache write unreported.

        Zero where the Attempt is unknown or predates the gateway metering:
        an old row without the counter reads back as exact, never as a bound.
        """
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.get_responses_with_unreported_cache_write(
                self._connection, attempt_id
            )

    def get_latest_usage(self, attempt_id: str) -> dict[str, Any] | None:
        """The newest ``usage`` record payload for one Attempt, if any.

        The durable read-back of the Attempt usage flush
        (:meth:`record_usage`): a Worker restart resumes the Attempt meter
        from the last flush instead of restarting from zero.
        """
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.get_latest_usage(self._connection, attempt_id)

    @property
    def registry(self) -> object:
        """The redaction registry every stored row passes through (§18)."""
        return self._registry

    def _archive_dir_for(self, attempt_id: str) -> Path | None:
        """The Attempt Archive directory, or ``None`` where it cannot resolve.

        Resolution needs the Target Repository from the operator instance row
        and a well-formed Attempt id; unit-test ledgers without an instance
        row get ``None`` and the caller falls back to the database event
        alone. Never raises: archive evidence must not fail a lifecycle
        transition.
        """
        target_repository = read_instance_repository(self._connection)
        if not target_repository:
            return None
        try:
            return attempt_archives.archive_dir_for(
                self.path.parent, target_repository, attempt_id
            )
        except ValueError:
            return None

    def archive_dir_for(self, attempt_id: str) -> Path | None:
        """The Attempt Archive directory, or ``None`` where it cannot resolve.

        The public form of :meth:`_archive_dir_for` for the step-workflow
        graph node (contract §4.5, S9) and the Worker: the node persists
        the Execution Summary and Step Artifacts into the archive the
        ledger resolves, and the Worker resolves ``AgentStepDeps.archive_dir``
        once per Attempt from it. Never raises.
        """
        try:
            return self._archive_dir_for(attempt_id)
        except Exception:
            return None

    def _phase_for_event(self, attempt_id: str, text: str) -> str | None:
        """The phase for a ``phase_changed`` event, best-effort."""
        try:
            row = self._connection.execute(
                "SELECT state FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is not None and row[0]:
                return str(row[0])
        except Exception:
            pass
        marker = " to "
        if marker in text:
            candidate = text.rsplit(marker, 1)[-1].strip()
            if candidate:
                return candidate
        return None

    def _outcome_for_event(self, attempt_id: str) -> tuple[str | None, str | None]:
        """The outcome and reason for an ``outcome_recorded`` event."""
        try:
            row = self._connection.execute(
                "SELECT outcome, reason FROM outcomes WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                outcome = str(row[0]) if row[0] is not None else None
                reason = str(row[1]) if row[1] is not None else None
                return (outcome, reason if reason else "")
        except Exception:
            pass
        return (None, None)

    def _emit_lifecycle_event(
        self,
        attempt_id: str,
        event_type: str,
        text: str,
        *,
        phase: str | None = None,
        outcome: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record one Attempt-scoped Contract Event, best-effort.

        The database row goes through the ledger's own redaction registry via
        the single producer and the same record is appended to the Attempt's
        ``events.jsonl``. Never raises: a lifecycle transition already
        committed must not fail for its evidence. Contract ``v1.0.4`` extras
        are filled here so every event validates: ``claim_opened`` carries
        ``issue``, ``phase_changed`` carries ``phase``, ``outcome_recorded``
        carries ``outcome`` and ``reason``, and the finished/released/sealed
        events carry ``attempt_id`` (via the archive producer).
        """
        try:
            archive = self._archive_dir_for(attempt_id)
            if archive is not None:
                attempt_archives.ensure_attempt_archive(archive)
            issue: int | None = None
            resolved_phase = phase
            resolved_outcome = outcome
            resolved_reason = reason
            if event_type == attempt_archives.EVENT_CLAIM_OPENED:
                from coding_agent.attempt_id import AttemptId

                parsed_issue = AttemptId.try_parse(attempt_id)
                issue = parsed_issue.input_number if parsed_issue is not None else None
            elif event_type == attempt_archives.EVENT_PHASE_CHANGED:
                if resolved_phase is None:
                    resolved_phase = self._phase_for_event(attempt_id, text)
            elif event_type == attempt_archives.EVENT_OUTCOME_RECORDED:
                if resolved_outcome is None or resolved_reason is None:
                    out, rsn = self._outcome_for_event(attempt_id)
                    if resolved_outcome is None:
                        resolved_outcome = out
                    if resolved_reason is None:
                        resolved_reason = rsn
            record = attempt_archives.emit_attempt_event(
                self._connection,
                archive_dir=archive,
                attempt_id=attempt_id,
                event_type=event_type,
                text=text,
                registry=self._registry,
                phase=resolved_phase,
                issue=issue,
                outcome=resolved_outcome,
                reason=resolved_reason,
            )
            self._connection.commit()
            try:
                print_contract_event(record)
            except Exception:
                pass
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                pass

    def emit_attempt_event(
        self,
        attempt_id: str,
        event_type: str,
        text: str,
        *,
        phase: str | None = None,
    ) -> None:
        """Record one Attempt-scoped Contract Event from the running graph.

        The native-event path (a Review starting or ending, a Gate
        diversion): the database row and the ``events.jsonl``
        line in one call under the ledger lock, through the ledger's own
        registry. Best-effort like every archive write — never raises, so
        evidence can never fail the Attempt it describes.

        ``phase`` carries the step phase (``<step>#<seq>``) where the
        emitter knows it: the step graph passes its own phase for
        ``phase_changed`` events so the record matches the live view,
        instead of resolving the Attempt's lifecycle state.
        """
        with self._lock:
            self._emit_lifecycle_event(attempt_id, event_type, text, phase=phase)

    def _maybe_seal(self, attempt_id: str) -> None:
        """Seal ``attempt_id`` where it is final and fully settled (#256).

        Sealing happens once the Attempt is ``finished`` or ``released``
        and every recorded step effect is settled — each ``step_effects``
        row at ``receipt`` or ``not-performed`` (ADR 0028) — including
        late backfills. The readiness check reads the ledger's own tables
        here under the ledger lock; the sealing mechanics (the sealed-event
        write, the manifest build and its atomic write) live in
        :mod:`coding_agent.operator.archive`. An archive is Sealed exactly
        when its sealed manifest exists. Never raises: sealing is
        best-effort evidence and must not fail the finalization or receipt
        it follows.
        """
        try:
            with self._lock:
                archive = self._archive_dir_for(attempt_id)
                if archive is None:
                    return
                try:
                    row = self._connection.execute(
                        "SELECT state FROM attempts WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchone()
                except sqlite3.Error:
                    return
                if row is None or str(row[0]) not in ("finished", "released"):
                    return
                try:
                    step_effect_rows = self._connection.execute(
                        "SELECT status FROM step_effects WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchall()
                except sqlite3.Error:
                    return
                for step_effect_row in step_effect_rows:
                    if str(step_effect_row[0]) not in (
                        STEP_EFFECT_STATUS_RECEIPT,
                        STEP_EFFECT_STATUS_NOT_PERFORMED,
                    ):
                        return
                record = attempt_archives.seal_archive(
                    self._connection,
                    archive,
                    attempt_id,
                    self._registry,
                )
                if record is not None:
                    try:
                        print_contract_event(record)
                    except Exception:
                        pass
        except Exception:
            pass

    def _redacted_payload(
        self, payload: Mapping[str, object] | None
    ) -> dict[str, object]:
        """The payload with every registered secret replaced (§18, L3-ERR-11)."""
        return dict(self._registry.redact_json(dict(payload or {})))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def checkpoint(self) -> None:
        """Checkpoint the WAL with ``TRUNCATE`` after the last write (§17).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (one SQLite
        file on the state volume, in WAL mode). Every orderly Worker exit
        calls this after its last write, so a restarted or offline reader
        finds the committed state in the database file itself: the normal
        exit, the SIGTERM shutdown, and later the Halt paths. The call
        replays committed WAL frames into the database and truncates the
        ``-wal`` file to zero bytes. Settings (WAL, ``synchronous=FULL``)
        are unchanged.

        Raises :class:`sqlite3.OperationalError` where the checkpoint did
        not finish (``busy`` set or frames left uncheckpointed), so a
        best-effort caller swallows consciously instead of assuming
        success. Uncontended — the Worker exiting with its own connection
        — the checkpoint completes.
        """
        with self._lock:
            row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert row is not None
        busy, log_frames, checkpointed = int(row[0]), int(row[1]), int(row[2])
        if busy != 0 or checkpointed != log_frames:
            raise sqlite3.OperationalError(
                f"wal_checkpoint(TRUNCATE) incomplete: busy={busy} "
                f"checkpointed={checkpointed}/{log_frames}"
            )

    def journal_mode(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        return str(row[0]).lower()

    def synchronous(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA synchronous").fetchone()
        assert row is not None
        return str(row[0])

    def table_names(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def open_attempt(
        self,
        attempt_id: str,
        *,
        effective_configuration: Mapping[str, object],
        fingerprint: str | None = None,
        base_revision: str | None = None,
        agent_identity_id: int | None = None,
        assigned_by_us: bool | None = None,
    ) -> AttemptRecord:
        """Open ``#<issue>/<n>`` in state ``open`` with its pinned configuration.

        The configuration is what the Attempt was opened with: under
        ``agent run`` the full Effective Configuration record
        (``workflow.effective.build_effective_configuration``) the Worker's
        Claim pins (contract §2.10, §5, CFG-41). The
        digest is over the stored bytes' canonical JSON form, so the stored
        digest equals the Effective Configuration's own digest.
        """
        settings = dict(effective_configuration)
        digest = digest_effective_configuration(settings)
        content_refs = self._content_index_for(settings)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO attempts
                    (attempt_id, state, effective_configuration,
                     effective_configuration_digest, fingerprint, base_revision,
                     agent_identity_id, assigned_by_us, content_refs, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    OPEN,
                    json.dumps(settings, sort_keys=True),
                    digest,
                    fingerprint,
                    base_revision,
                    agent_identity_id,
                    None if assigned_by_us is None else int(assigned_by_us),
                    json.dumps(content_refs, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
            self._emit_lifecycle_event(
                attempt_id,
                attempt_archives.EVENT_CLAIM_OPENED,
                f"claim opened {attempt_id}",
            )
        return self.get_attempt(attempt_id)

    def has_attempt(self, attempt_id: str) -> bool:
        """Whether ``attempt_id`` already has its ``attempts`` row (§5).

        The Worker's Claim opens the row before the graph runs; the graph's
        ``claim`` node then runs that Attempt instead of opening another.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return row is not None

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        with self._lock:
            try:
                row = self._connection.execute(
                    """
                    SELECT attempt_id, state, effective_configuration,
                           effective_configuration_digest, fingerprint, base_revision,
                           agent_identity_id, assigned_by_us, content_refs
                    FROM attempts WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = self._connection.execute(
                    """
                    SELECT attempt_id, state, effective_configuration,
                           effective_configuration_digest, fingerprint, base_revision,
                           agent_identity_id, assigned_by_us
                    FROM attempts WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
        assert row is not None, f"no attempts row for {attempt_id}"
        return self._attempt_from_row(row)

    @staticmethod
    def _attempt_from_row(row: object) -> AttemptRecord:
        """Rebuild an :class:`AttemptRecord` from an ``attempts`` row tuple."""
        from coding_agent.ledger.queries import attempt_from_row

        return attempt_from_row(row)

    def unfinished_attempts(self) -> tuple[AttemptRecord, ...]:
        """Every Attempt startup recovery must settle, in ledger order (§17).

        Contract: ``docs/contract/mvp-runtime-contract.md`` §17 (recovery
        runs at startup, before any selection, for every unfinished Attempt)
        and §3 (intake: while one exists the Worker selects no new work).

        MVP row ids: REC-CLM-1, REC-CLM-2, REC-WRK-1.

        An Attempt is unfinished while its state is neither ``finished`` nor
        ``released``. The ``effective_configuration`` JSON of a row the
        database corrupted raises here; the caller turns that into a startup
        refusal (REC-CFG-2) instead of guessing the configuration.
        """
        with self._lock:
            try:
                rows = self._connection.execute(
                    """
                    SELECT attempt_id, state, effective_configuration,
                           effective_configuration_digest, fingerprint, base_revision,
                           agent_identity_id, assigned_by_us, content_refs
                    FROM attempts
                    WHERE state NOT IN ('finished', 'released')
                    ORDER BY rowid
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self._connection.execute(
                    """
                    SELECT attempt_id, state, effective_configuration,
                           effective_configuration_digest, fingerprint, base_revision,
                           agent_identity_id, assigned_by_us
                    FROM attempts
                    WHERE state NOT IN ('finished', 'released')
                    ORDER BY rowid
                    """
                ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def attempt_opened_at(self, attempt_id: str) -> datetime:
        """When the Attempt opened in the Run Ledger (§11, L3-IMP-7).

        The wall clock is measured from this timestamp, never from process
        start, so review time counts too. A missing or unparseable timestamp
        falls back to now, yielding a zero elapsed rather than a failure.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT created_at FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def record_usage(
        self, attempt_id: str, snapshot: BudgetSnapshot, *, detail: str | None = None
    ) -> LedgerRecord:
        """Flush cumulative usage as a readable ``usage`` record (§11, C-LIM-1).

        The payload carries the cumulative totals plus per-bucket tokens and
        dollars (``UsageTotals.as_readable``), so the terminal comment and an
        operator report the spend directly. Called after every model response
        and every tool result, and once more before the hard-ceiling
        finalization.
        """
        payload: dict[str, object] = {
            "usage": snapshot.usage.as_readable(),
            "elapsed_seconds": snapshot.elapsed_seconds,
        }
        if snapshot.context is not None:
            payload["context"] = dict(snapshot.context)
        if snapshot.by_origin is not None:
            payload["by_origin"] = {
                origin: cell.as_readable()
                for origin, cell in snapshot.by_origin.items()
            }
        return self.record(
            attempt_id,
            USAGE_KIND,
            snapshot,
            detail=detail or "attempt usage flush",
            payload=payload,
        )

    def set_attempt_state(self, attempt_id: str, state: AttemptState) -> None:
        """Mark an Attempt's lifecycle state, completing a pending stop.

        The transition to ``finished`` or ``released`` completes a pending
        ``stop`` in the same write transaction (#249): the drain holds
        across the normal loop, the infrastructure-failure path, the
        Claim read-back release and every recovery settling path. Intake
        stays closed; completing never reopens it. The same transaction
        also resolves a pending ``next`` priority for this Attempt's
        issue: a priority resolved by a failed Claim read-back is
        resolved here (#252). A ``finished`` with a recorded Terminal
        Outcome also counts toward a ``stop --after`` plan in the same
        transaction; a ``released`` Claim never counts.

        A repeated terminal transition is a no-op: moving an Attempt to
        ``finished`` or ``released`` when it already holds that state
        emits no event, counts nothing toward a stop plan, resolves
        nothing and never reseals — the Worker finalises a live Attempt
        before the poll loop finalises it again, and the archive sealed
        by the first transition must never change.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                # The idempotency read shares the write transaction: a read
                # failure rolls back and raises below like any other
                # database failure, so a transition we cannot prove is a
                # repeat never runs its terminal effects half-blind.
                current_row = self._connection.execute(
                    "SELECT state FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                current_state: str | None = (
                    str(current_row[0])
                    if current_row is not None and current_row[0] is not None
                    else None
                )
                if state in ("finished", "released") and current_state == state:
                    self._connection.rollback()
                    return
                self._connection.execute(
                    "UPDATE attempts SET state = ? WHERE attempt_id = ?",
                    (state, attempt_id),
                )
                if state in ("finished", "released"):
                    complete_pending_stop(
                        self._connection, registry=self._registry
                    )
                    resolve_matching_next_on_finalize(
                        self._connection,
                        attempt_id,
                        registry=self._registry,
                    )
                if state == "finished":
                    count_stop_plan(
                        self._connection,
                        attempt_id,
                        registry=self._registry,
                    )
                self._connection.commit()
                try:

                    flush_pending_events(self._connection)
                except Exception:
                    pass
                if state == "finished":
                    self._emit_lifecycle_event(
                        attempt_id,
                        attempt_archives.EVENT_ATTEMPT_FINISHED,
                        f"attempt finished {attempt_id}",
                    )
                elif state == "released":
                    self._emit_lifecycle_event(
                        attempt_id,
                        attempt_archives.EVENT_ATTEMPT_RELEASED,
                        f"attempt released {attempt_id}",
                    )
                else:
                    self._emit_lifecycle_event(
                        attempt_id,
                        attempt_archives.EVENT_PHASE_CHANGED,
                        f"phase changed {attempt_id} to {state}",
                    )
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                try:

                    discard_pending_events(self._connection)
                except Exception:
                    pass
                raise
        if state in ("finished", "released"):
            self._maybe_seal(attempt_id)

    def open_attempt_if_intake_open(
        self,
        attempt_id: str,
        *,
        effective_configuration: Mapping[str, object],
        fingerprint: str | None = None,
        base_revision: str | None = None,
        agent_identity_id: int | None = None,
        assigned_by_us: bool | None = None,
    ) -> AttemptRecord | None:
        """Open ``#<issue>/<n>`` only where durable Intake is open (#249).

        The Intake check and the commit of the new Attempt row happen in
        one immediate write transaction (the Claim boundary): a ``stop``
        committed before the boundary prevents the Claim from opening,
        while one committed after stays ``accepted`` until this Attempt
        finalizes. Returns ``None`` where Intake is closed, having
        written nothing. The GitHub assignment and its read-back stay in
        ``worker.claim``.
        """
        settings = dict(effective_configuration)
        digest = digest_effective_configuration(settings)
        content_refs = self._content_index_for(settings)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                intake = read_intake(self._connection)
                if intake.get("state") != "open":
                    self._connection.rollback()
                    return None
                try:
                    self._connection.execute(
                        """
                        INSERT INTO attempts
                            (attempt_id, state, effective_configuration,
                             effective_configuration_digest, fingerprint, base_revision,
                             agent_identity_id, assigned_by_us, content_refs, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            OPEN,
                            json.dumps(settings, sort_keys=True),
                            digest,
                            fingerprint,
                            base_revision,
                            agent_identity_id,
                            None if assigned_by_us is None else int(assigned_by_us),
                            json.dumps(content_refs, sort_keys=True),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                except sqlite3.OperationalError:
                    self._connection.execute(
                        """
                        INSERT INTO attempts
                            (attempt_id, state, effective_configuration,
                             effective_configuration_digest, fingerprint, base_revision,
                             agent_identity_id, assigned_by_us, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            OPEN,
                            json.dumps(settings, sort_keys=True),
                            digest,
                            fingerprint,
                            base_revision,
                            agent_identity_id,
                            None if assigned_by_us is None else int(assigned_by_us),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                self._connection.commit()
                self._emit_lifecycle_event(
                    attempt_id,
                    attempt_archives.EVENT_CLAIM_OPENED,
                    f"claim opened {attempt_id}",
                )
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                raise
        return self.get_attempt(attempt_id)

    def intake_is_open(self) -> bool:
        """Whether durable Intake is open (absent means open, #249)."""
        with self._lock:
            return read_intake(self._connection).get("state") == "open"

    def reconcile_accepted_commands(self) -> None:
        """Resolve ``accepted`` commands left by an earlier process (#249).

        Runs before the first Claim boundary: every ``accepted`` command
        whose Attempt is already final becomes ``completed``. A pending
        stop with an unfinished Attempt stays pending so the drain still
        holds after a restart. A pending ``next`` priority stays
        ``accepted`` until a Claim boundary tries it (#252).
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                reconcile_accepted(self._connection, registry=self._registry)
                self._connection.commit()
                try:

                    flush_pending_events(self._connection)
                except Exception:
                    pass
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                try:

                    discard_pending_events(self._connection)
                except Exception:
                    pass
                raise

    def pending_next_command(self) -> CommandRecord | None:
        """The pending ``next issue N`` priority, if any (#252).

        Returns the newest ``accepted`` ``next`` command record, or
        ``None`` where no priority waits for the next Claim boundary.
        """
        with self._lock:
            return get_pending_next_command(self._connection)

    def resolve_next_command(
        self, request_id: str, acknowledgement: str, *, detail: str
    ) -> bool:
        """Resolve one accepted priority in its own write transaction (#252).

        Emits ``command_resolved`` beside the update. Returns ``True``
        where an ``accepted`` row was resolved.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                resolved = _resolve(
                    self._connection,
                    request_id,
                    acknowledgement,
                    detail=detail,
                    registry=self._registry,
                )
                self._connection.commit()
                try:

                    flush_pending_events(self._connection)
                except Exception:
                    pass
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                try:

                    discard_pending_events(self._connection)
                except Exception:
                    pass
                raise
        return resolved

    def confirm_priority_claim(
        self, attempt_id: str, request_id: str, *, detail: str
    ) -> None:
        """Confirm a priority Claim and complete its priority at once (#252).

        One write transaction marks the Attempt ``claim-confirmed`` and
        resolves the priority ``completed`` with its ``command_resolved``
        event, so a confirmed Claim never leaves its priority pending.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE attempts SET state = ? WHERE attempt_id = ?",
                    (CLAIM_CONFIRMED, attempt_id),
                )
                _resolve(
                    self._connection,
                    request_id,
                    "completed",
                    detail=detail,
                    registry=self._registry,
                )
                self._connection.commit()
                try:

                    flush_pending_events(self._connection)
                except Exception:
                    pass
                self._emit_lifecycle_event(
                    attempt_id,
                    attempt_archives.EVENT_PHASE_CHANGED,
                    f"phase changed {attempt_id} to claim-confirmed",
                )
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                try:

                    discard_pending_events(self._connection)
                except Exception:
                    pass
                raise

    def release_priority_claim(
        self, attempt_id: str, request_id: str, *, detail: str
    ) -> None:
        """Release a priority Claim and resolve it ``not_fulfilled`` (#252).

        One write transaction — the finalization transaction the Claim
        read-back releases in — marks the Attempt ``released``,
        completes a pending ``stop`` like every other release, and
        resolves the priority ``not_fulfilled`` with its
        ``command_resolved`` event.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "UPDATE attempts SET state = ? WHERE attempt_id = ?",
                    (RELEASED, attempt_id),
                )
                complete_pending_stop(
                    self._connection, registry=self._registry
                )
                _resolve(
                    self._connection,
                    request_id,
                    "not_fulfilled",
                    detail=detail,
                    registry=self._registry,
                )
                self._connection.commit()
                try:

                    flush_pending_events(self._connection)
                except Exception:
                    pass
                self._emit_lifecycle_event(
                    attempt_id,
                    attempt_archives.EVENT_ATTEMPT_RELEASED,
                    f"attempt released {attempt_id}",
                )
            except Exception:
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                try:

                    discard_pending_events(self._connection)
                except Exception:
                    pass
                raise
        self._maybe_seal(attempt_id)

    def set_assigned_by_us(self, attempt_id: str, assigned: bool) -> None:
        """Record whether the Agent Identity's assignment is ours (§5).

        Written only after the Worker performs the assignment, so the
        Attempt row exists before the assignment is made and recovery can
        still release an unconfirmed Claim (REC-CLM-1).
        """
        with self._lock:
            self._connection.execute(
                "UPDATE attempts SET assigned_by_us = ? WHERE attempt_id = ?",
                (int(assigned), attempt_id),
            )
            self._connection.commit()

    def set_attempt_claim_pins(
        self, attempt_id: str, *, fingerprint: str, base_revision: str
    ) -> None:
        """Carry the Claim read-back pins on the ``attempts`` row (§7).

        Step-workflow contract §7: the Fingerprint and Base Revision pinned
        at Claim travel with the Attempt, beside the Effective Configuration
        the Claim already records. Additive: one ``UPDATE`` over existing
        columns, so ledgers and readers from earlier slices are untouched.
        """
        with self._lock:
            self._connection.execute(
                "UPDATE attempts SET fingerprint = ?, base_revision = ?"
                " WHERE attempt_id = ?",
                (fingerprint, base_revision, attempt_id),
            )
            self._connection.commit()

    def next_workflow_attempt_number(
        self, workflow_id: str, input_number: int
    ) -> int:
        """One more than the highest ``n`` for a (Workflow, input) (§6.3).

        Step-workflow contract §6.3: an Attempt is ``<workflow>#<N>/<n>`` and
        ``n`` counts per (Workflow, input) over every recorded Attempt.
        Numbers are never reused, not even after a ``released`` Claim.
        After a ledger loss the caller takes the maximum of this, the
        Attempt branches and the Attempt Markers on GitHub (§16.5, REC-22).
        """
        prefix = f"{workflow_id}#{input_number}/"
        with self._lock:
            rows = self._connection.execute(
                "SELECT attempt_id FROM attempts"
            ).fetchall()
        highest = 0
        for row in rows:
            stored = str(row[0])
            if not stored.startswith(prefix):
                continue
            suffix = stored[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return highest + 1

    def record_attempt_pin(self, pin: AttemptPin) -> AttemptPin:
        """Record the input state pinned at Claim, once (§5.1, §6, §7).

        The first record wins: a repeated Claim for the same Attempt id
        returns the stored pin, so recovery never re-pins a confirmed
        Attempt. ``n`` is never reused (§6.3), so a repeated record for one
        id always carries the same pins.
        """
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO attempt_pins
                    (attempt_id, workflow_id, input_type, input_number,
                     title, body, fingerprint, base_revision,
                     base_tip, input_head, head_branch, base_branch,
                     selection_actor, selection_actor_id, selection_actor_type,
                     selection_actor_at, identity_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pin.attempt_id,
                    pin.workflow_id,
                    pin.input_type,
                    pin.input_number,
                    pin.title,
                    pin.body,
                    pin.fingerprint,
                    pin.base_revision,
                    pin.base_tip,
                    pin.input_head,
                    pin.head_branch,
                    pin.base_branch,
                    pin.selection_actor,
                    pin.selection_actor_id,
                    pin.selection_actor_type,
                    pin.selection_actor_at,
                    pin.identity_mode,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
        stored = self.get_attempt_pin(pin.attempt_id)
        assert stored is not None
        return stored

    def get_attempt_pin(self, attempt_id: str) -> AttemptPin | None:
        """Return the recorded pins for one Attempt, if any (§7)."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT attempt_id, workflow_id, input_type, input_number,
                       title, body, fingerprint, base_revision,
                       base_tip, input_head, head_branch, base_branch,
                       selection_actor, selection_actor_id, selection_actor_type,
                       selection_actor_at, identity_mode
                FROM attempt_pins WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        assert isinstance(row, tuple)
        return AttemptPin(
            attempt_id=str(row[0]),
            workflow_id=str(row[1]),
            input_type=str(row[2]),
            input_number=int(row[3]),
            title=str(row[4]),
            body=str(row[5]),
            fingerprint=str(row[6]),
            base_revision=str(row[7]),
            base_tip=str(row[8]) if row[8] is not None else None,
            input_head=str(row[9]) if row[9] is not None else None,
            head_branch=str(row[10]) if row[10] is not None else None,
            base_branch=str(row[11]) if row[11] is not None else None,
            selection_actor=str(row[12]) if row[12] is not None else None,
            selection_actor_id=int(row[13]) if row[13] is not None else None,
            selection_actor_type=str(row[14]) if row[14] is not None else None,
            selection_actor_at=str(row[15]) if row[15] is not None else None,
            identity_mode=str(row[16]) if row[16] is not None else None,
        )

    def record_pinned_comments(
        self,
        attempt_id: str,
        comments: Sequence[PinnedComment],
        *,
        issue_author_id: int | None,
        owner_id: int | None,
        agent_id: int | None,
    ) -> str:
        """Record the Trusted Comments pinned at Claim, once (§7.4, ADR 0035).

        One ``pinned_comment_sets`` row (the whole-set digest, the count and
        the three numeric identity ids the selector ran under, so the
        terminal comment can disclose late comments) plus one
        ``pinned_input_comments`` row per comment (id, role, author id,
        timestamps, full body raw like the input pins, content digest and
        the publishing Workflow id). The first record wins: ``INSERT OR
        IGNORE`` on both tables, so recovery never re-pins a confirmed
        Attempt. Returns the whole-set digest. Attempts recorded before
        this change simply have no set row: the getters read back ``None``.
        """
        from coding_agent.inputs.comments import comments_set_digest

        set_digest = comments_set_digest(comments)
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO pinned_comment_sets
                    (attempt_id, set_digest, comment_count,
                     issue_author_id, owner_id, agent_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    set_digest,
                    len(comments),
                    issue_author_id,
                    owner_id,
                    agent_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            for comment in comments:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO pinned_input_comments
                        (attempt_id, comment_id, role, author_id,
                         created_at, updated_at, edited, body, digest,
                         workflow_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        comment.comment_id,
                        comment.role,
                        comment.author_id,
                        comment.created_at,
                        comment.updated_at,
                        1 if comment.edited else 0,
                        comment.body,
                        comment.digest,
                        comment.workflow_id,
                    ),
                )
            self._connection.commit()
        return set_digest

    def get_pinned_comment_set(self, attempt_id: str) -> PinnedCommentSet | None:
        """Return the pinned comment set record for one Attempt, if any (§7.4).

        ``None`` where no set row exists: the Attempt predates Trusted
        Comment pins or its Workflow declares no ``input.comments`` step —
        both read as absent, never as a failure.
        """
        with self._lock:
            try:
                row = self._connection.execute(
                    """
                    SELECT attempt_id, set_digest, comment_count,
                           issue_author_id, owner_id, agent_id
                    FROM pinned_comment_sets WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            return None
        assert isinstance(row, tuple)
        return PinnedCommentSet(
            attempt_id=str(row[0]),
            set_digest=str(row[1]),
            comment_count=int(row[2]),
            issue_author_id=int(row[3]) if row[3] is not None else None,
            owner_id=int(row[4]) if row[4] is not None else None,
            agent_id=int(row[5]) if row[5] is not None else None,
        )

    def get_pinned_comments(self, attempt_id: str) -> list[PinnedComment] | None:
        """Return the pinned Trusted Comments for one Attempt, if any (§7.4).

        ``None`` where no set row exists (an older Attempt, or a Workflow
        with no declaring step); otherwise the pinned comments oldest
        first — an empty list where the pin held no Trusted Comments.
        """
        if self.get_pinned_comment_set(attempt_id) is None:
            return None
        with self._lock:
            try:
                rows = self._connection.execute(
                    """
                    SELECT comment_id, role, author_id, created_at,
                           updated_at, edited, body, digest, workflow_id
                    FROM pinned_input_comments
                    WHERE attempt_id = ? ORDER BY comment_id ASC
                    """,
                    (attempt_id,),
                ).fetchall()
            except sqlite3.Error:
                return None
        pinned: list[PinnedComment] = []
        for row in rows:
            assert isinstance(row, tuple)
            pinned.append(
                PinnedComment(
                    comment_id=int(row[0]),
                    role=str(row[1]),
                    author_id=int(row[2]) if row[2] is not None else None,
                    created_at=str(row[3]),
                    updated_at=str(row[4]) if row[4] is not None else None,
                    edited=bool(row[5]),
                    body=str(row[6]),
                    digest=str(row[7]),
                    workflow_id=str(row[8]) if row[8] is not None else None,
                )
            )
        return pinned

    def record_merge_pending(
        self,
        attempt_id: str,
        *,
        base_tip: str,
        auto_tree: str,
        conflicted_paths: tuple[str, ...] | list[str],
    ) -> None:
        """Record the pending second parent of a pull-request Attempt (§10.8).

        Written by ``update-from-base`` after the merge; read back by
        ``commit-and-push`` and by recovery. One row per Attempt: recording
        again replaces the earlier row.
        """
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO attempt_merge_state
                    (attempt_id, base_tip, auto_tree, conflicted_paths, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    base_tip = excluded.base_tip,
                    auto_tree = excluded.auto_tree,
                    conflicted_paths = excluded.conflicted_paths,
                    created_at = excluded.created_at
                """,
                (
                    attempt_id,
                    base_tip,
                    auto_tree,
                    json.dumps(list(conflicted_paths)),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()

    def get_merge_pending(self, attempt_id: str) -> MergePending | None:
        """Return the pending second parent of one Attempt, if any (§10.8)."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT attempt_id, base_tip, auto_tree, conflicted_paths
                FROM attempt_merge_state WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        assert isinstance(row, tuple)
        try:
            raw_paths = json.loads(str(row[3]))
        except (json.JSONDecodeError, TypeError, ValueError):
            raw_paths = []
        paths = tuple(str(entry) for entry in raw_paths) if isinstance(raw_paths, list) else ()
        return MergePending(
            attempt_id=str(row[0]),
            base_tip=str(row[1]),
            auto_tree=str(row[2]),
            conflicted_paths=paths,
        )

    def clear_merge_pending(self, attempt_id: str) -> None:
        """Discard the pending second parent of one Attempt (§10.8).

        Used when the merge commit that consumes it is created. An Attempt
        that ends without pushing leaves its row behind; a new Attempt id
        never reuses it.
        """
        with self._lock:
            self._connection.execute(
                "DELETE FROM attempt_merge_state WHERE attempt_id = ?",
                (attempt_id,),
            )
            self._connection.commit()

    def has_unfinished_attempt(self) -> bool:
        """Whether intake must wait: an Attempt neither finished nor released (§3)."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.has_unfinished_attempt(self._connection)

    def record_terminal_outcome(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason: str,
        pull_request_body: str = "",
        terminal_comment_body: str = "",
        writes: tuple[str, ...] | list[str] = (),
    ) -> OutcomeRecord:
        """Record the Terminal Outcome **before** any tracker write (§14).

        Moves the Attempt to ``outcome-recorded`` and inserts its ``outcomes``
        row in the same transaction discipline as every later slice: recovery
        finishes a recorded outcome and never re-decides it. The row carries
        the rendered comment and pull-request bodies and the list of writes it
        calls for, so a retried or recovered write sends the same bytes. At
        most one outcome row exists per Attempt; a second call leaves the
        first in place and returns it, so a retried record sends the same
        bytes.

        Also records the ``outcome_recorded`` Attempt-scoped Contract Event
        (adoption step 4, #255): the database row plus the Attempt's
        ``events.jsonl`` line, best-effort so evidence never fails the
        transition.
        """
        with self._lock:
            existing = self._connection.execute(
                "SELECT attempt_id, outcome, reason, pull_request_body,"
                " terminal_comment_body, writes FROM outcomes WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                return self._outcome_from_row(existing)
            ordered_writes = list(writes)
            redacted_reason = self._registry.redact(reason)
            redacted_pr_body = self._registry.redact(pull_request_body)
            redacted_comment = self._registry.redact(terminal_comment_body)
            self._connection.execute(
                """
                INSERT INTO outcomes
                    (attempt_id, outcome, reason, pull_request_body,
                     terminal_comment_body, writes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    outcome,
                    redacted_reason,
                    redacted_pr_body,
                    redacted_comment,
                    json.dumps(ordered_writes, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.execute(
                "UPDATE attempts SET state = ? WHERE attempt_id = ?",
                (OUTCOME_RECORDED, attempt_id),
            )
            self._connection.commit()
            self._emit_lifecycle_event(
                attempt_id,
                attempt_archives.EVENT_OUTCOME_RECORDED,
                f"outcome recorded {attempt_id} {outcome}: {redacted_reason}",
            )
        return OutcomeRecord(
            attempt_id=attempt_id,
            outcome=outcome,
            reason=redacted_reason,
            pull_request_body=redacted_pr_body,
            terminal_comment_body=redacted_comment,
            writes=tuple(ordered_writes),
        )

    @staticmethod
    def _outcome_from_row(row: object) -> OutcomeRecord:
        """Rebuild an :class:`OutcomeRecord` from an ``outcomes`` row tuple."""
        from coding_agent.ledger.queries import outcome_from_row

        return outcome_from_row(row)

    def record_halt(
        self, *, reason: str, detail: str, occurred_at: str | None = None
    ) -> dict[str, object]:
        """Record the durable Halt and its ``halted`` event before exiting (#250).

        Every non-zero exit the Worker decides on after taking the volume lock
        records here: the reason, the redacted detail (capped at 300
        characters) and the time, plus a ``halted`` Contract Event in the
        same file. A restart keeps the row, reporting it as
        ``instance.last_halt``. Contract shape (``v1.0.4``): ``{reason,
        detail, at}`` with ``at`` in RFC 3339 UTC; stored rows from older
        volumes (``+00:00``) convert on read. Uses this ledger's redaction
        registry.
        """
        moment_z = to_z(occurred_at) if occurred_at is not None else utc_now_z()
        if moment_z is None:
            moment_z = utc_now_z()
        redacted = self._registry.redact(detail)
        if len(redacted) > MAX_EVENT_TEXT_LEN:
            redacted = redacted[:MAX_EVENT_TEXT_LEN]
        process_started = moment_z
        try:
            stored_started = read_process_started_at(self._connection)
            if stored_started:
                process_started = stored_started
        except Exception:
            pass
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_halt (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                INSERT INTO operator_halt (id, reason, detail, occurred_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    reason = excluded.reason,
                    detail = excluded.detail,
                    occurred_at = excluded.occurred_at
                """,
                (reason, redacted, moment_z),
            )
            halt_record: dict[str, object] | None = None
            try:
                halt_record = dict(
                    emit_contract_event(
                        self._connection,
                        process_started_at=process_started,
                        event_type=EVENT_HALTED,
                        text=f"halted {reason}: {redacted}",
                        registry=self._registry,
                        level=LEVEL_ERROR,
                        reason=reason,
                    )
                )
            except Exception:
                halt_record = None
            try:
                self._connection.commit()
            except Exception:
                halt_record = None
            if halt_record is not None:
                try:
                    print_contract_event(halt_record)
                except Exception:
                    pass
        return {
            "reason": reason,
            "detail": redacted,
            "at": moment_z,
        }

    def emit_contract_event(
        self,
        *,
        process_started_at: str,
        event_type: str,
        text: str,
    ) -> dict[str, object]:
        """Record one Contract Event through this ledger's registry (#250)."""
        with self._lock:
            record = emit_contract_event(
                self._connection,
                process_started_at=process_started_at,
                event_type=event_type,
                text=text,
                registry=self._registry,
            )
            self._connection.commit()
            try:
                print_contract_event(record)
            except Exception:
                pass
        return dict(record)

    def get_outcome(self, attempt_id: str) -> OutcomeRecord | None:
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.get_outcome(self._connection, attempt_id)

    def record(
        self,
        attempt_id: str,
        kind: str,
        snapshot: BudgetSnapshot,
        *,
        detail: str | None = None,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> LedgerRecord:
        remaining = snapshot.remaining(self.ceilings)
        event_payload = self._redacted_payload(payload)
        redacted_detail = self._registry.redact(detail) if detail is not None else None
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO run_ledger_events
                    (attempt_id, kind, detail, artifact_ref, diff_ref,
                     remaining_budget, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    kind,
                    redacted_detail,
                    artifact_ref,
                    diff_ref,
                    json.dumps(remaining, sort_keys=True),
                    json.dumps(event_payload, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
            row = self._connection.execute("SELECT last_insert_rowid()").fetchone()
        assert row is not None
        return LedgerRecord(
            id=int(row[0]),
            attempt_id=attempt_id,
            kind=kind,
            detail=redacted_detail,
            artifact_ref=artifact_ref,
            diff_ref=diff_ref,
            remaining_budget=remaining,
            payload=event_payload,
        )

    def records(self, attempt_id: str) -> tuple[LedgerRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, attempt_id, kind, detail, artifact_ref, diff_ref,
                       remaining_budget, payload
                FROM run_ledger_events
                WHERE attempt_id = ?
                ORDER BY id
                """,
                (attempt_id,),
            ).fetchall()
        return tuple(
            LedgerRecord(
                id=int(row[0]),
                attempt_id=str(row[1]),
                kind=str(row[2]),
                detail=row[3],
                artifact_ref=row[4],
                diff_ref=row[5],
                remaining_budget=json.loads(row[6]),
                payload=json.loads(row[7]),
            )
            for row in rows
        )

    # --- Step-workflow recording (contract §13.2, §14.1; slice S2) ---

    @staticmethod
    def _step_execution_from_row(row: object) -> StepExecutionRecord:
        """Rebuild a :class:`StepExecutionRecord` from a row tuple."""
        from coding_agent.ledger.queries import step_execution_from_row

        return step_execution_from_row(row)

    def open_step_execution(
        self,
        attempt_id: str,
        step_id: str,
        seq: int,
        *,
        entry_from_step: str | None = None,
        entry_result: str | None = None,
    ) -> StepExecutionRecord:
        """Write a Step Execution opening before the step starts (§13.2).

        Matrix row REC-1: the opening (step, sequence number, entry Route)
        is committed before the step runs. Re-opening an existing key
        returns the stored row, so a retried open writes nothing new.
        """
        existing = self.get_step_execution(attempt_id, step_id, seq)
        if existing is not None:
            return existing
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO step_executions
                    (attempt_id, step_id, seq, entry_from_step, entry_result,
                     state, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    step_id,
                    seq,
                    entry_from_step,
                    entry_result,
                    STEP_EXECUTION_OPEN,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
        record = self.get_step_execution(attempt_id, step_id, seq)
        assert record is not None
        return record

    def end_step_execution(
        self,
        attempt_id: str,
        step_id: str,
        seq: int,
        *,
        result: str,
        payload: Mapping[str, object] | None = None,
        facts: Mapping[str, object] | None = None,
        usage: Mapping[str, object] | None = None,
        limit_fact: Mapping[str, object] | None = None,
    ) -> StepExecutionRecord:
        """Commit a Step Execution end before the router runs (§13.2).

        Matrix row REC-1: the end (result with payload, facts, usage, any
        ``limit-exceeded`` fact) is committed before the router selects a
        successor, so a crash between the end and the transition still
        records what the step decided.
        """
        with self._lock:
            self._connection.execute(
                """
                UPDATE step_executions
                SET state = ?, result = ?, payload = ?, facts = ?,
                    usage = ?, limit_fact = ?, ended_at = ?
                WHERE attempt_id = ? AND step_id = ? AND seq = ?
                """,
                (
                    STEP_EXECUTION_ENDED,
                    result,
                    json.dumps(self._redacted_payload(payload), sort_keys=True)
                    if payload is not None
                    else None,
                    json.dumps(self._redacted_payload(facts), sort_keys=True)
                    if facts is not None
                    else None,
                    json.dumps(self._redacted_payload(usage), sort_keys=True)
                    if usage is not None
                    else None,
                    json.dumps(self._redacted_payload(limit_fact), sort_keys=True)
                    if limit_fact is not None
                    else None,
                    datetime.now(timezone.utc).isoformat(),
                    attempt_id,
                    step_id,
                    seq,
                ),
            )
            self._connection.commit()
        record = self.get_step_execution(attempt_id, step_id, seq)
        assert record is not None, f"no step_executions row for {attempt_id}/{step_id}/{seq}"
        return record

    def get_step_execution(
        self, attempt_id: str, step_id: str, seq: int
    ) -> StepExecutionRecord | None:
        """Return one ``step_executions`` row by its ``(Attempt, step, n)`` key."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            for record in _ledger_queries.list_step_executions(
                self._connection, attempt_id
            ):
                if record.step_id == step_id and record.seq == seq:
                    return record
        return None

    def list_step_executions(self, attempt_id: str) -> tuple[StepExecutionRecord, ...]:
        """Return every ``step_executions`` row for one Attempt, in open order."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.list_step_executions(self._connection, attempt_id)

    def close_interrupted_step_executions(
        self, attempt_id: str, *, reason: str = "process-loss"
    ) -> int:
        """Close every open Step Execution as interrupted (§13.2, §16.1).

        Recovery, never routing: the rows become a runtime fact — never a
        Step Result — and no Workflow Step runs (matrix row REC-12, owned by
        S8; the ledger primitive lands here). Returns the count closed.
        """
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE step_executions
                SET state = ?, ended_at = ?
                WHERE attempt_id = ? AND state = ?
                """,
                (
                    STEP_EXECUTION_INTERRUPTED,
                    datetime.now(timezone.utc).isoformat(),
                    attempt_id,
                    STEP_EXECUTION_OPEN,
                ),
            )
            self._connection.commit()
            closed = cursor.rowcount
        if closed:
            self.record(
                attempt_id,
                "step_execution_interrupted",
                BudgetSnapshot(elapsed_seconds=0.0, usage=UsageTotals()),
                detail=f"{closed} Step Execution(s) interrupted by {reason}",
                payload={"reason": self._registry.redact(reason)},
            )
        return int(closed)

    def record_step_transition(
        self,
        attempt_id: str,
        *,
        kind: str,
        from_step: str | None = None,
        result: str | None = None,
        to_step: str | None = None,
        to_end: str | None = None,
        reason: str | None = None,
    ) -> StepTransitionRecord:
        """Record the Route taken, a refused entry or a Workflow End (§13.2).

        Matrix row REC-2 (owned by S3): a refused ``execution-ceiling``
        entry creates no Step Execution — it records only this transition
        fact naming the step and the reason.
        """
        redacted_reason = self._registry.redact(reason) if reason is not None else None
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO step_transitions
                    (attempt_id, from_step, result, to_step, to_end, kind,
                     reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    from_step,
                    result,
                    to_step,
                    to_end,
                    kind,
                    redacted_reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
            row = self._connection.execute("SELECT last_insert_rowid()").fetchone()
        assert row is not None
        return StepTransitionRecord(
            id=int(row[0]),
            attempt_id=attempt_id,
            kind=kind,
            from_step=from_step,
            result=result,
            to_step=to_step,
            to_end=to_end,
            reason=redacted_reason,
        )

    def record_route_taken(
        self, attempt_id: str, *, from_step: str, result: str, to_step: str
    ) -> StepTransitionRecord:
        """Record the Route taken from ``from_step`` on ``result`` (§13.2)."""
        return self.record_step_transition(
            attempt_id,
            kind=STEP_TRANSITION_ROUTE,
            from_step=from_step,
            result=result,
            to_step=to_step,
        )

    def record_refused_entry(
        self, attempt_id: str, *, step_id: str, reason: str
    ) -> StepTransitionRecord:
        """Record a refused entry with no Step Execution (§8.3, §13.2).

        A Route into a step at its Execution Ceiling creates no Step
        Execution and the sequence does not advance; routing continues
        along that step's ``limit-exceeded`` Route.
        """
        return self.record_step_transition(
            attempt_id,
            kind=STEP_TRANSITION_REFUSED,
            to_step=step_id,
            reason=reason,
        )

    def record_workflow_end(
        self, attempt_id: str, *, from_step: str, result: str, end: str
    ) -> StepTransitionRecord:
        """Record the Workflow End reached (§8.2, §13.2)."""
        return self.record_step_transition(
            attempt_id,
            kind=STEP_TRANSITION_END,
            from_step=from_step,
            result=result,
            to_end=end,
        )

    def list_step_transitions(self, attempt_id: str) -> tuple[StepTransitionRecord, ...]:
        """Return every ``step_transitions`` row for one Attempt, in order."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.list_step_transitions(self._connection, attempt_id)

    def record_admission_hold(
        self,
        attempt_id: str,
        step_id: str,
        seq: int,
        hold_id: str,
        *,
        bound_tokens: int,
        bound_cost_usd: float,
    ) -> AdmissionHoldRecord:
        """Hold the worst-case bound while a request is in flight (§11.4).

        Matrix row LIM-7: the hold is in force while the request runs, so
        concurrent Sub-agents cannot both pass on the same headroom. An
        existing ``hold_id`` returns its row: a retried admission holds
        nothing twice.
        """
        existing = self.get_admission_hold(hold_id)
        if existing is not None:
            return existing
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO admission_holds
                    (attempt_id, step_id, seq, hold_id, bound_tokens,
                     bound_cost_usd, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    step_id,
                    seq,
                    hold_id,
                    bound_tokens,
                    bound_cost_usd,
                    ADMISSION_HOLD_HELD,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT id FROM admission_holds WHERE hold_id = ?", (hold_id,)
            ).fetchone()
        assert row is not None
        record = self.get_admission_hold(hold_id)
        assert record is not None
        return record

    def settle_admission_hold(
        self, hold_id: str, *, status: str = ADMISSION_HOLD_SETTLED
    ) -> AdmissionHoldRecord | None:
        """Settle a hold with the actual usage, or charge its worst case.

        Matrix row LIM-8: a response without usage (successful or failed),
        and a hold still open after a crash, are each charged their
        admitted worst case (``charged``); the normal path settles
        (``settled``).
        """
        with self._lock:
            self._connection.execute(
                "UPDATE admission_holds SET status = ?, settled_at = ? WHERE hold_id = ?",
                (status, datetime.now(timezone.utc).isoformat(), hold_id),
            )
            self._connection.commit()
        return self.get_admission_hold(hold_id)

    def get_admission_hold(self, hold_id: str) -> AdmissionHoldRecord | None:
        """Return one ``admission_holds`` row by its hold id."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, attempt_id, step_id, seq, hold_id, bound_tokens,
                       bound_cost_usd, status
                FROM admission_holds
                WHERE hold_id = ?
                """,
                (hold_id,),
            ).fetchone()
        if row is None:
            return None
        return AdmissionHoldRecord(
            id=int(row[0]),
            attempt_id=str(row[1]),
            step_id=str(row[2]),
            seq=int(row[3]),
            hold_id=str(row[4]),
            bound_tokens=int(row[5]),
            bound_cost_usd=float(row[6]),
            status=str(row[7]),
        )

    def list_open_admission_holds(
        self, attempt_id: str
    ) -> tuple[AdmissionHoldRecord, ...]:
        """Return every still-``held`` admission hold for one Attempt."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, attempt_id, step_id, seq, hold_id, bound_tokens,
                       bound_cost_usd, status
                FROM admission_holds
                WHERE attempt_id = ? AND status = ?
                ORDER BY id
                """,
                (attempt_id, ADMISSION_HOLD_HELD),
            ).fetchall()
        return tuple(
            AdmissionHoldRecord(
                id=int(row[0]),
                attempt_id=str(row[1]),
                step_id=str(row[2]),
                seq=int(row[3]),
                hold_id=str(row[4]),
                bound_tokens=int(row[5]),
                bound_cost_usd=float(row[6]),
                status=str(row[7]),
            )
            for row in rows
        )

    def charge_open_admission_holds(self, attempt_id: str) -> int:
        """Charge every still-open hold its worst case (§11.4, §13.2).

        Restart settlement for holds a crash left open (matrix row LIM-8,
        owned by S5/S8; the ledger primitive lands here). Returns the count
        charged.
        """
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE admission_holds
                SET status = ?, settled_at = ?
                WHERE attempt_id = ? AND status = ?
                """,
                (
                    ADMISSION_HOLD_CHARGED,
                    datetime.now(timezone.utc).isoformat(),
                    attempt_id,
                    ADMISSION_HOLD_HELD,
                ),
            )
            self._connection.commit()
            return int(cursor.rowcount)

    @staticmethod
    @staticmethod
    def _step_effect_from_row(row: object) -> StepEffectRecord:
        """Rebuild a :class:`StepEffectRecord` from a ``step_effects`` row tuple."""
        from coding_agent.ledger.queries import step_effect_from_row

        return step_effect_from_row(row)

    def record_step_effect_intent(
        self,
        attempt_id: str,
        step_id: str,
        execution_number: int,
        ordinal: int,
        *,
        rendered_payload: str,
        expected_target: Mapping[str, object],
    ) -> StepEffectRecord:
        """Record one external write's intent, keyed ``(Attempt, step, n, ordinal)``.

        Step-workflow contract §14.1 (matrix row REC-4): written after the
        Gate, immediately before the write, carrying the exact rendered
        payload and the expected target state. Repeating the same key
        returns the existing row, so re-entering an intent writes nothing
        new; a row already at ``receipt`` stays a receipt.
        """
        existing = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        if existing is not None:
            return existing
        redacted_payload = self._registry.redact(rendered_payload)
        redacted_target = self._redacted_payload(expected_target)
        digest = digest_effect_payload(
            {"rendered_payload": redacted_payload, **redacted_target}
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO step_effects
                    (attempt_id, step_id, execution_number, ordinal, status,
                     rendered_payload, expected_target, payload_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    step_id,
                    execution_number,
                    ordinal,
                    STEP_EFFECT_STATUS_INTENT,
                    redacted_payload,
                    json.dumps(redacted_target, sort_keys=True),
                    digest,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._connection.commit()
        record = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        assert record is not None
        return record

    def record_step_effect_receipt(
        self,
        attempt_id: str,
        step_id: str,
        execution_number: int,
        ordinal: int,
        *,
        remote_id: str | None = None,
    ) -> StepEffectRecord:
        """Record the receipt of one external write (§14.1).

        The remote sha, pull-request number, review id or comment id lands
        here when the write does. A receipt with no intent row inserts one,
        so a remote that already holds our key is recognised as ours.
        """
        existing = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        if existing is not None and existing.status == STEP_EFFECT_STATUS_RECEIPT:
            # Re-entering a receipt pushes nothing, but a best-effort seal
            # may have failed earlier, so retry it before returning.
            self._maybe_seal(attempt_id)
            return existing
        with self._lock:
            if existing is None:
                digest = digest_effect_payload({})
                self._connection.execute(
                    """
                    INSERT INTO step_effects
                        (attempt_id, step_id, execution_number, ordinal, status,
                         rendered_payload, expected_target, remote_id, payload_digest,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        step_id,
                        execution_number,
                        ordinal,
                        STEP_EFFECT_STATUS_RECEIPT,
                        "",
                        "{}",
                        remote_id,
                        digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                kept_remote = remote_id if remote_id is not None else existing.remote_id
                self._connection.execute(
                    """
                    UPDATE step_effects
                    SET status = ?, remote_id = ?
                    WHERE attempt_id = ? AND step_id = ?
                      AND execution_number = ? AND ordinal = ?
                    """,
                    (
                        STEP_EFFECT_STATUS_RECEIPT,
                        kept_remote,
                        attempt_id,
                        step_id,
                        execution_number,
                        ordinal,
                    ),
                )
            self._connection.commit()
        record = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        assert record is not None
        self._maybe_seal(attempt_id)
        return record

    def record_step_effect_not_performed(
        self,
        attempt_id: str,
        step_id: str,
        execution_number: int,
        ordinal: int,
        *,
        reason: str,
    ) -> StepEffectRecord:
        """Record an intent settled as ``not-performed`` with its reason (§14.2)."""
        existing = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        redacted_reason = self._registry.redact(reason)
        with self._lock:
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO step_effects
                        (attempt_id, step_id, execution_number, ordinal, status,
                         rendered_payload, expected_target, remote_id, payload_digest,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        step_id,
                        execution_number,
                        ordinal,
                        STEP_EFFECT_STATUS_NOT_PERFORMED,
                        redacted_reason,
                        "{}",
                        None,
                        digest_effect_payload({"reason": redacted_reason}),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE step_effects
                    SET status = ?, rendered_payload = ?
                    WHERE attempt_id = ? AND step_id = ?
                      AND execution_number = ? AND ordinal = ?
                    """,
                    (
                        STEP_EFFECT_STATUS_NOT_PERFORMED,
                        redacted_reason,
                        attempt_id,
                        step_id,
                        execution_number,
                        ordinal,
                    ),
                )
            self._connection.commit()
        record = self.get_step_effect(attempt_id, step_id, execution_number, ordinal)
        assert record is not None
        self._maybe_seal(attempt_id)
        return record

    def get_step_effect(
        self, attempt_id: str, step_id: str, execution_number: int, ordinal: int
    ) -> StepEffectRecord | None:
        """Return one ``step_effects`` row by its intent key."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            for record in _ledger_queries.list_step_effects(
                self._connection, attempt_id
            ):
                if (
                    record.step_id == step_id
                    and record.execution_number == execution_number
                    and record.ordinal == ordinal
                ):
                    return record
        return None

    def list_step_effects(self, attempt_id: str) -> tuple[StepEffectRecord, ...]:
        """Return every ``step_effects`` row for one Attempt, in key order."""
        from coding_agent.ledger import queries as _ledger_queries

        with self._lock:
            return _ledger_queries.list_step_effects(self._connection, attempt_id)
