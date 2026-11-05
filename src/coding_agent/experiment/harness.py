"""The experiment harness: interleaved paid Attempts with per-run evidence.

Runs outside ``agent implement``, driven by a validated
:class:`~coding_agent.experiment.protocol.ExperimentProtocol`. The harness
runs the Attempts sequentially in the protocol's declared order and writes
one JSON document per run plus an aggregate (per arm x fixture: values,
median, spread).

Before each run the harness refuses to start unless the run would begin
from an identical state: the Target Issue body matches its digest, the
Selection Label is present and the Base Revision is checked out with no
leftover Attempt branch. It restores the checkout state itself where it can
(checking out the Base Revision, resetting, cleaning and deleting leftover
Attempt branches); a body or label mismatch cannot be restored and refuses
the run. A run whose per-Attempt limit, added to the spend so far, would
exceed the overall cap is never started: the harness stops and records why.

After each Attempt ends the harness runs the fixture's oracle command
against the captured final working tree, outside the Attempt. Oracle files
live outside the workspace and are materialised into the snapshot copy only
after the Attempt ends, so they never appear in the workspace during it.

The per-run working-tree diff is taken against the fixture's Base Revision,
not ``HEAD``: Attempts commit to the Attempt branch before review and during
delivery, so a ``HEAD``-based diff would hide everything committed. New files
appear as ordinary hunks with their content; ignored bootstrap artifacts
stay out via the final tree's ignore rules.

Only this module (and the runtime seam it exercises) constructs the control
exit policy. The ``agent implement`` entry path and Workflow Configuration
loading never import this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from coding_agent.experiment.oracle_access import (
    build_oracle_access_check,
    oracle_access_signals,
)
from coding_agent.experiment.protocol import (
    ExperimentProtocol,
    ProtocolArm,
    ProtocolFixture,
)
from coding_agent.identity.commit_identity import ATTEMPT_BRANCH_PREFIX
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.runtime.ceilings import LoopCeilings
from coding_agent.runtime.redaction import Redactable
from coding_agent.runtime.transcript import (
    TRANSCRIPT_FILE_NAME,
    TranscriptModel,
    TranscriptWriter,
)
from coding_agent.runtime.completion import (
    ClaimRecord,
    CriterionEvidence,
    normalize_claim,
)
from coding_agent.runtime.exit_policy import (
    DEFAULT_STEP_EXIT_POLICY,
    SILENCE_ENDS_STAGE_EXIT_POLICY,
    StepExitPolicy,
    tool_schema_digest,
)
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.review_snapshot import ReviewSnapshotState
from coding_agent.tracker.port import TrackerPort

ORACLE_OUTPUT_LIMIT = 4000
"""Chars of oracle stdout/stderr kept per run; the rest is cut with a marker."""

DIFF_TEXT_LIMIT = 65536
"""Chars of working-tree diff kept per run; the rest is cut with a marker."""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...truncated ({len(text) - limit} chars omitted)"


def policy_for_arm(arm: ProtocolArm) -> StepExitPolicy:
    """The step exit policy an arm runs under (ADR 0019)."""
    if arm.exit_policy == "silence_ends_stage":
        return SILENCE_ENDS_STAGE_EXIT_POLICY
    return DEFAULT_STEP_EXIT_POLICY


def digest_skill_bundle(skills_dir: Path) -> str:
    """Digest the Skill Bundle bytes the Attempts activate from."""
    digest = hashlib.sha256()
    if not skills_dir.is_dir():
        digest.update(b"missing-skills-dir")
        return digest.hexdigest()
    paths = sorted(
        (path for path in skills_dir.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix(),
    )
    for path in paths:
        try:
            relative = path.relative_to(skills_dir).as_posix()
        except ValueError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\0")
    return digest.hexdigest()


def median(values: Sequence[float]) -> float | None:
    """The median of ``values``, or ``None`` where there are none."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    middle = count // 2
    if count % 2 == 1:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def spread(values: Sequence[float]) -> float | None:
    """The spread (max minus min) of ``values``, or ``None`` where empty."""
    if not values:
        return None
    return float(max(values)) - float(min(values))


class _RecordingLog:
    """Shared per-run record of what the model was offered and what it did."""

    def __init__(self) -> None:
        self.surfaces: list[tuple[str | None, list[BaseTool]]] = []
        self.invocations = 0
        self.activation_trace: list[str] = []
        self.report_completion_calls: list[dict[str, Any]] = []

    def note_binding(
        self, tool_choice: str | None, tools: Sequence[BaseTool]
    ) -> None:
        """Record one ``bind_tools`` tool surface, in order."""
        self.surfaces.append((tool_choice, list(tools)))

    def note_response(self, response: BaseMessage) -> None:
        """Record one model response's tool calls, in order."""
        self.invocations += 1
        if not isinstance(response, AIMessage):
            return
        for call in response.tool_calls:
            name = str(call.get("name", ""))
            if name.startswith("activate_"):
                self.activation_trace.append(name)
            if name == "report_completion":
                args = call.get("args")
                self.report_completion_calls.append(
                    dict(args) if isinstance(args, dict) else {}
                )


class RecordingModel:
    """An :class:`InvokableToolModel` recording surfaces and responses.

    Wraps the run's real model (scripted in tests, pinned in production) and
    delegates every call, so the Attempt runs exactly as it would unwrapped;
    the recording is the harness's observation of the tool schema actually
    offered, the activation trace and the Completion Claim calls.
    """

    def __init__(
        self, inner: InvokableToolModel, log: _RecordingLog | None = None
    ) -> None:
        self._inner = inner
        self._log = log if log is not None else _RecordingLog()

    @property
    def log(self) -> _RecordingLog:
        """The shared per-run recording."""
        return self._log

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> RecordingModel:
        """Bind ``tools`` on the wrapped model, recording the surface."""
        self._log.note_binding(tool_choice, tools)
        if tool_choice is None:
            bound = self._inner.bind_tools(tools)
        else:
            bound = self._inner.bind_tools(tools, tool_choice=tool_choice)
        return RecordingModel(bound, self._log)

    def invoke(self, input: Any) -> BaseMessage:
        """Invoke the wrapped model, recording the response's tool calls."""
        response = self._inner.invoke(input)
        assert isinstance(response, BaseMessage)
        self._log.note_response(response)
        return response


@dataclass
class AttemptObservation:
    """Everything the harness observed about one Attempt, for its record."""

    terminal_outcome: str
    terminal_reason: str
    claim: dict[str, list[str] | None] | None
    claim_force_accepted: bool
    claim_calls: int
    implementation_outcome: str | None
    implementation_reason: str | None
    evidence: list[tuple[str, bool]]
    model_calls: int
    tool_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    elapsed_seconds: float | None
    activation_trace: list[str]
    tool_surface_names: list[str]
    tool_schema: str
    review_ran: bool
    review_status: str
    review_provenance: str
    working_tree_digest: str
    working_tree_diff: str
    working_tree_diff_empty: bool
    oracle_exit_code: int | None
    oracle_stdout: str
    oracle_stderr: str
    oracle_timed_out: bool


_IMPLEMENTATION_OUTCOME_FOR_REASON: dict[str, tuple[str | None, str | None]] = {
    "verified-completion": ("verified completion", "verified-completion"),
    "unverified": ("implemented but unverified", "unverified"),
    "already-satisfied": ("already satisfied", "already-satisfied"),
    "criteria-uncovered": ("completion not established", "criteria-uncovered"),
    "no-completion-evidence": (
        "completion not established",
        "no-completion-evidence",
    ),
    "claims-unverified": ("completion not established", "claims-unverified"),
    "undeclared": ("completion not established", "undeclared"),
}
"""Terminal reasons that carry their Implementation Outcome with them.

Cutoff and refusal reasons (ceilings, stalls, the reserve, readiness and
infrastructure failures) decide no Implementation Outcome: the harness
records those as explicitly missing, never as zero or a guess.
"""


def implementation_outcome_for(
    terminal_outcome: str, terminal_reason: str
) -> tuple[str | None, str | None]:
    """Map a Terminal Outcome back to its Implementation Outcome, if any."""
    _ = terminal_outcome
    return _IMPLEMENTATION_OUTCOME_FOR_REASON.get(terminal_reason, (None, None))


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def workspace_head(workspace: Path) -> str | None:
    """The workspace's current ``HEAD`` sha, or ``None`` where unreadable."""
    result = _git(["rev-parse", "HEAD"], workspace)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def attempt_branches(workspace: Path, issue_number: int) -> list[str]:
    """Leftover Attempt branches for the issue, by the runtime's prefix."""
    result = _git(
        [
            "branch",
            "--list",
            f"{ATTEMPT_BRANCH_PREFIX}/{issue_number}/*",
            "--format=%(refname:short)",
        ],
        workspace,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def restore_workspace(workspace: Path, base_revision: str) -> list[str]:
    """Restore the identical start state, returning what was removed.

    Checks out the Base Revision, resets hard, cleans untracked files and
    deletes leftover Attempt branches. Callers re-read the state and refuse
    the run where it still mismatches.
    """
    _git(["checkout", "-q", base_revision], workspace)
    removed: list[str] = []
    # List every leftover Attempt branch regardless of issue: the prefix
    # alone scopes the runtime's branches, and a run must not inherit any.
    listed = _git(
        [
            "branch",
            "--list",
            f"{ATTEMPT_BRANCH_PREFIX}/*",
            "--format=%(refname:short)",
        ],
        workspace,
    )
    if listed.returncode == 0:
        for branch in listed.stdout.splitlines():
            name = branch.strip()
            if not name or name.startswith("*"):
                continue
            deleted = _git(["branch", "-D", name], workspace)
            if deleted.returncode == 0:
                removed.append(name)
    _git(["reset", "-q", "--hard", base_revision], workspace)
    _git(["clean", "-q", "-fdx"], workspace)
    return removed


def tree_digest(workspace: Path) -> str:
    """Digest the working tree (tracked and untracked), excluding ``.git``."""
    digest = hashlib.sha256()
    paths = sorted(
        (
            path
            for path in workspace.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ),
        key=lambda path: path.as_posix(),
    )
    for path in paths:
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\0")
    return digest.hexdigest()


def tree_diff(workspace: Path, base_revision: str) -> tuple[str, bool]:
    """The final working tree diffed against the fixture's Base Revision.

    Stages the current working tree (tracked and untracked, minus ignored)
    into a temporary index and diffs that index against ``base_revision``,
    so Attempt commits that moved ``HEAD`` make no difference. New files
    appear as ordinary hunks with their full content, not as status lines;
    ignored bootstrap artifacts stay out via the final tree's ignore rules.
    Read-only against the workspace: the real index, ``HEAD`` and branches
    are left untouched.
    """
    fd, tmp_index = tempfile.mkstemp(prefix="experiment-tree-diff-")
    os.close(fd)
    try:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = tmp_index
        subprocess.run(
            ["git", "add", "-A", "--", "."],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        diff = subprocess.run(
            ["git", "diff", "--cached", base_revision, "--", "."],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        diff_text = diff.stdout if diff.returncode == 0 else ""
        is_empty = diff_text.strip() == ""
        return (_truncate(diff_text, DIFF_TEXT_LIMIT), is_empty)
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


def copy_tree_without_git(source: Path, destination: Path) -> None:
    """Copy the working tree for the oracle, never carrying ``.git``."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git"),
    )


@dataclass
class OracleResult:
    """The oracle command's outcome against the captured tree."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def run_oracle(
    command: Sequence[str], cwd: Path, timeout_seconds: float
) -> OracleResult:
    """Run the fixture's oracle command against the captured tree."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return OracleResult(
            exit_code=None,
            stdout=_truncate(stdout, ORACLE_OUTPUT_LIMIT),
            stderr=_truncate(stderr, ORACLE_OUTPUT_LIMIT),
            timed_out=True,
        )
    except OSError as exc:
        return OracleResult(
            exit_code=None,
            stdout="",
            stderr=_truncate(f"oracle failed to start: {exc}", ORACLE_OUTPUT_LIMIT),
            timed_out=False,
        )
    return OracleResult(
        exit_code=completed.returncode,
        stdout=_truncate(completed.stdout, ORACLE_OUTPUT_LIMIT),
        stderr=_truncate(completed.stderr, ORACLE_OUTPUT_LIMIT),
        timed_out=False,
    )


@dataclass
class ExperimentDeps:
    """Everything the harness needs that is not the protocol itself.

    Tests inject scripted models and fake trackers here; production passes
    the Pinned Model and the live tracker. ``make_model`` builds the run's
    model from ``(arm_id, fixture_id, run_index)``. ``redact`` carries the
    redaction registry the Transcript writer and the Attempt share, in any
    :data:`Redactable` form; ``None`` means no redaction.
    """

    tracker: TrackerPort
    workspace: Path
    skills_dir: Path
    out_dir: Path
    effective_configuration: Mapping[str, object]
    make_model: Callable[[str, str, int], InvokableToolModel]
    runtime_commit: str = "unknown"
    skill_bundle_digest: str | None = None
    oracle_timeout_seconds: float = 300.0
    price: TokenPrices | None = None
    clock: Callable[[], float] = time.monotonic
    redact: Redactable = None


def _ledger_usage(ledger: RunLedger, attempt_id: str) -> tuple[dict[str, Any] | None, float | None]:
    """The latest flushed usage payload and elapsed seconds, if any."""
    latest: dict[str, Any] | None = None
    elapsed: float | None = None
    for record in ledger.records(attempt_id):
        if record.kind != "usage":
            continue
        payload = dict(record.payload)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            latest = {str(key): value for key, value in usage.items()}
        raw_elapsed = payload.get("elapsed_seconds")
        if isinstance(raw_elapsed, (int, float)):
            elapsed = float(raw_elapsed)
    return latest, elapsed


def _review_from_ledger(ledger: RunLedger, attempt_id: str) -> tuple[bool, str] | None:
    """``(ran, status)`` from the latest ledger review-status event, if any."""
    latest: tuple[bool, str] | None = None
    for record in ledger.records(attempt_id):
        if record.kind != "review_status":
            continue
        payload = dict(record.payload)
        status = payload.get("status")
        invocations = payload.get("invocations")
        if isinstance(status, str) and isinstance(invocations, int):
            latest = (invocations > 0, status)
    return latest


def _optional_int(usage: dict[str, Any] | None, key: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_float(usage: dict[str, Any] | None, key: str) -> float | None:
    if usage is None:
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _criteria_ids(body: str) -> tuple[str, ...]:
    from coding_agent.runtime.acceptance import parse_acceptance_criteria

    try:
        return tuple(criterion.id for criterion in parse_acceptance_criteria(body))
    except Exception:
        return ()


STATUS_COMPLETED = "completed"
STATUS_ATTEMPT_ERROR = "attempt-error"
STATUS_PRECONDITION_REFUSED = "precondition-refused"
STATUS_CAP_REFUSED = "cap-refused"
STATUS_VOIDED = "voided"


def _base_record(
    *,
    status: str,
    seq: int,
    arm: ProtocolArm,
    fixture: ProtocolFixture,
    run_index: int,
    runtime_commit: str,
    skill_bundle_digest: str,
    pinned_model: Mapping[str, object],
    per_attempt_cost_usd: float,
    spend_before_usd: float,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "seq": seq,
        "pins": {
            "arm": arm.id,
            "exit_policy": arm.exit_policy,
            "fixture": fixture.id,
            "issue_number": fixture.issue_number,
            "run_index": run_index,
            "runtime_commit": runtime_commit,
            "skill_bundle_digest": skill_bundle_digest,
            "pinned_model": {
                "provider": pinned_model.get("provider"),
                "model": pinned_model.get("model"),
                "effort": pinned_model.get("effort"),
            },
            "tool_schema_digest": None,
            "per_attempt_cost_usd": per_attempt_cost_usd,
        },
        "preconditions": {
            "issue_body_digest_expected": fixture.issue_body_digest,
            "issue_body_digest_actual": None,
            "issue_body_match": None,
            "selection_label": None,
            "selection_label_present": None,
            "base_revision_expected": fixture.base_revision,
            "base_revision_actual": None,
            "base_revision_match": None,
            "leftover_branches_removed": [],
            "oracle_files_absent": None,
        },
        "behaviour": {
            "activation_trace": [],
            "claim": None,
            "claim_calls": 0,
            "claim_force_accepted": False,
            "implementation_outcome": None,
            "implementation_reason": None,
            "terminal_outcome": None,
            "terminal_reason": None,
            "criterion_evidence": [],
            "review": {
                "ran": False,
                "status": None,
                "provenance": None,
            },
        },
        "meters": {
            "model_calls": None,
            "tool_calls": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cost_usd": None,
            "elapsed_seconds": None,
        },
        "result": {
            "working_tree_digest": None,
            "working_tree_diff": None,
            "working_tree_diff_empty": None,
            "oracle": {
                "command": list(fixture.oracle),
                "exit_code": None,
                "stdout": None,
                "stderr": None,
                "timed_out": None,
            },
        },
        "spend": {
            "spend_before_usd": spend_before_usd,
            "spend_after_usd": spend_before_usd,
        },
        "oracle_access_check": {
            "checked": False,
            "transcript_digest": None,
            "matches": [],
        },
        "refusal_reason": None,
    }


def _aggregate_cell(
    records: list[dict[str, Any]], arm_id: str, fixture_id: str
) -> dict[str, Any]:
    meter_keys = (
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "elapsed_seconds",
    )
    cell = [
        record
        for record in records
        if record.get("status") == STATUS_COMPLETED
        and record["pins"]["arm"] == arm_id
        and record["pins"]["fixture"] == fixture_id
    ]
    voided = sum(
        1
        for record in records
        if record.get("status") == STATUS_VOIDED
        and record["pins"]["arm"] == arm_id
        and record["pins"]["fixture"] == fixture_id
    )
    summary: dict[str, Any] = {
        "arm": arm_id,
        "fixture": fixture_id,
        "runs": len(cell),
        "voided": voided,
        "terminal": sorted(
            str(record["behaviour"]["terminal_outcome"])
            + "/"
            + str(record["behaviour"]["terminal_reason"])
            for record in cell
        ),
        "meters": {},
    }
    meters_summary = summary["meters"]
    assert isinstance(meters_summary, dict)
    for key in meter_keys:
        values: list[float] = []
        missing = 0
        for record in cell:
            value = record["meters"].get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                missing += 1
            else:
                values.append(float(value))
        meters_summary[key] = {
            "values": values,
            "missing": missing,
            "median": median(values),
            "spread": spread(values),
        }
    return summary


def build_aggregate(
    protocol: ExperimentProtocol, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """The aggregate: per arm x fixture values, median and spread."""
    return {
        "schema_version": 1,
        "cells": [
            _aggregate_cell(records, arm.id, fixture.id)
            for arm in protocol.arms
            for fixture in protocol.fixtures
        ],
    }


@dataclass
class ExperimentReport:
    """The harness's outcome: per-run records, the aggregate and the spend."""

    records: list[dict[str, Any]] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    spend_total_usd: float = 0.0
    halted: dict[str, Any] | None = None
    out_dir: Path | None = None


def run_experiment(
    protocol: ExperimentProtocol, deps: ExperimentDeps
) -> ExperimentReport:
    """Run the protocol's declared order, writing one JSON record per run.

    The protocol must already be validated (see
    :func:`~coding_agent.experiment.protocol.parse_protocol`); validation
    happens before any paid call because this function performs none itself
    beyond the Attempts it runs in order.
    """
    out_dir = deps.out_dir
    runs_dir = out_dir / "runs"
    ledgers_dir = out_dir / "ledgers"
    snapshots_dir = out_dir / "snapshots"
    for directory in (runs_dir, ledgers_dir, snapshots_dir):
        directory.mkdir(parents=True, exist_ok=True)
    skill_digest = deps.skill_bundle_digest
    if skill_digest is None:
        skill_digest = digest_skill_bundle(deps.skills_dir)

    report = ExperimentReport(out_dir=out_dir)
    spend = 0.0
    seq = 0
    halted: dict[str, Any] | None = None
    signals = oracle_access_signals(protocol)

    for entry in protocol.order:
        seq += 1
        arm = protocol.arm_by_id(entry.arm)
        fixture = protocol.fixture_by_id(entry.fixture)
        record = _base_record(
            status=STATUS_COMPLETED,
            seq=seq,
            arm=arm,
            fixture=fixture,
            run_index=entry.run,
            runtime_commit=deps.runtime_commit,
            skill_bundle_digest=skill_digest,
            pinned_model={
                "provider": protocol.pinned_model.provider,
                "model": protocol.pinned_model.model,
                "effort": protocol.pinned_model.effort,
            },
            per_attempt_cost_usd=protocol.per_attempt_cost_usd,
            spend_before_usd=spend,
        )
        # The overall cap is checked before any state is touched: a run
        # whose per-Attempt limit would carry the spend over the cap is
        # never started, and the experiment stops here.
        if spend + protocol.per_attempt_cost_usd > protocol.overall_cap_usd + 1e-9:
            record["status"] = STATUS_CAP_REFUSED
            record["refusal_reason"] = (
                f"spend ${spend:.4f} plus per-Attempt limit "
                f"${protocol.per_attempt_cost_usd:.4f} would exceed overall cap "
                f"${protocol.overall_cap_usd:.4f}"
            )
            _write_record(runs_dir, record)
            report.records.append(record)
            halted = {
                "seq": seq,
                "arm": arm.id,
                "fixture": fixture.id,
                "run_index": entry.run,
                "reason": record["refusal_reason"],
            }
            break

        refusal = _check_preconditions(deps, record, fixture)
        if refusal is not None:
            record["status"] = STATUS_PRECONDITION_REFUSED
            record["refusal_reason"] = refusal
            _write_record(runs_dir, record)
            report.records.append(record)
            continue

        _run_one_attempt(
            deps, protocol, arm, fixture, entry.run, seq, record, signals
        )
        cost = record["meters"].get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            cost = 0.0
        spend = spend + float(cost)
        record["spend"]["spend_after_usd"] = spend
        _write_record(runs_dir, record)
        report.records.append(record)

    report.spend_total_usd = spend
    report.halted = halted
    report.aggregate = build_aggregate(protocol, report.records)
    (out_dir / "aggregate.json").write_text(
        json.dumps(report.aggregate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    voided = [
        runs_dir.name + "/" + _record_name(record)
        for record in report.records
        if record.get("status") == STATUS_VOIDED
    ]
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runs": len(report.records),
                "spend_total_usd": spend,
                "halted": halted,
                "records": [runs_dir.name + "/" + _record_name(record) for record in report.records],
                "voided": voided,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


def _record_name(record: dict[str, Any]) -> str:
    pins = record["pins"]
    return (
        f"run-{int(record['seq']):03d}-{pins['arm']}-"
        f"{pins['fixture']}-r{int(pins['run_index'])}.json"
    )


def _write_record(runs_dir: Path, record: dict[str, Any]) -> None:
    (runs_dir / _record_name(record)).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _check_preconditions(
    deps: ExperimentDeps, record: dict[str, Any], fixture: ProtocolFixture
) -> str | None:
    """Refuse the run unless it would begin from an identical state.

    Returns the refusal reason, or ``None`` where the run may start. The
    checkout state is restored where it can be (checking out the Base
    Revision, resetting, cleaning, deleting leftover Attempt branches); a
    body or label mismatch cannot be restored and refuses the run.
    """
    from coding_agent.experiment.protocol import issue_body_digest

    try:
        issue = deps.tracker.fetch_issue(fixture.issue_number)
    except Exception as exc:
        return f"could not fetch Target Issue #{fixture.issue_number}: {exc}"
    actual_digest = issue_body_digest(issue.body)
    record["preconditions"]["issue_body_digest_actual"] = actual_digest
    record["preconditions"]["issue_body_match"] = (
        actual_digest == fixture.issue_body_digest
    )
    if actual_digest != fixture.issue_body_digest:
        return (
            f"Target Issue #{fixture.issue_number} body digest {actual_digest} "
            f"does not match protocol {fixture.issue_body_digest}"
        )
    try:
        state = deps.tracker.read_issue_state(fixture.issue_number)
    except Exception as exc:
        return f"could not read issue state #{fixture.issue_number}: {exc}"
    selection_label = deps.tracker.selection_label
    record["preconditions"]["selection_label"] = selection_label
    present = selection_label in tuple(state.labels)
    record["preconditions"]["selection_label_present"] = present
    if not present:
        return (
            f"Selection Label {selection_label!r} absent from "
            f"Target Issue #{fixture.issue_number} labels {list(state.labels)!r}"
        )
    oracle_names = [name for name in fixture.oracle_files]
    clashes = [
        name
        for name in oracle_names
        if (deps.workspace / name).exists()
    ]
    record["preconditions"]["oracle_files_absent"] = not clashes
    if clashes:
        return f"oracle files already present in workspace: {clashes!r}"
    removed = restore_workspace(deps.workspace, fixture.base_revision)
    record["preconditions"]["leftover_branches_removed"] = removed
    head = workspace_head(deps.workspace)
    record["preconditions"]["base_revision_actual"] = head
    record["preconditions"]["base_revision_match"] = head == fixture.base_revision
    if head != fixture.base_revision:
        return (
            f"Base Revision {head!r} does not match protocol "
            f"{fixture.base_revision!r} after restore"
        )
    leftovers = attempt_branches(deps.workspace, fixture.issue_number)
    if leftovers:
        return f"leftover Attempt branches remain: {leftovers!r}"
    return None


def _apply_oracle_access_check(
    record: dict[str, Any],
    transcript_path: Path,
    signals: Sequence[str],
) -> None:
    """Scan the run's Transcript and void the run on any match.

    Sets ``record["oracle_access_check"]`` and, where the scan matched or
    the Transcript is missing/unreadable, flips the status to ``voided``.
    Refused runs never reach here: they never started an Attempt.
    """
    check = build_oracle_access_check(transcript_path, signals)
    record["oracle_access_check"] = check
    if not check["checked"] or check["matches"]:
        record["status"] = STATUS_VOIDED


def _run_one_attempt(
    deps: ExperimentDeps,
    protocol: ExperimentProtocol,
    arm: ProtocolArm,
    fixture: ProtocolFixture,
    run_index: int,
    seq: int,
    record: dict[str, Any],
    signals: Sequence[str] | None = None,
) -> None:
    ledgers_dir = deps.out_dir / "ledgers"
    snapshots_dir = deps.out_dir / "snapshots"
    ledger_path = ledgers_dir / f"run-{seq:03d}.sqlite3"
    evidence_dir = deps.out_dir / "evidence" / f"run-{seq:03d}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = evidence_dir / TRANSCRIPT_FILE_NAME
    # An Attempt that never calls the model still leaves a Transcript behind
    # (empty, hence clean); only a run that removes or corrupts it cannot be
    # shown clean.
    transcript_path.touch(exist_ok=True)
    writer = TranscriptWriter(transcript_path, deps.redact)
    ledger = RunLedger(ledger_path)
    try:
        recording: RecordingModel
        evidence = CriterionEvidence()
        review_state = ReviewSnapshotState()
        claim_records: list[tuple[str, ClaimRecord]] = []
        attempt_id = f"#{fixture.issue_number}/1"
        try:
            inner = deps.make_model(arm.id, fixture.id, run_index)
            recording = RecordingModel(inner)
            model = TranscriptModel(recording, writer)
            result = run_attempt(
                issue_number=fixture.issue_number,
                attempt_number=1,
                tracker=deps.tracker,
                ledger=ledger,
                model=model,
                workspace=deps.workspace,
                effective_configuration=dict(deps.effective_configuration),
                skills_dir=deps.skills_dir,
                ceilings=LoopCeilings(
                    max_cost_usd=protocol.per_attempt_cost_usd
                ),
                price=deps.price,
                evidence_dir=evidence_dir,
                exit_policy=policy_for_arm(arm),
                criterion_evidence=evidence,
                review_state=review_state,
                claim_records=claim_records,
                registry=deps.redact,
                review_redact=deps.redact,
            )
        except Exception as exc:
            record["status"] = STATUS_ATTEMPT_ERROR
            record["refusal_reason"] = f"attempt raised: {exc}"
            _fill_workspace_result(deps, fixture, seq, record, with_oracle=False)
            _apply_oracle_access_check(
                record, transcript_path, signals or oracle_access_signals(protocol)
            )
            if record["status"] == STATUS_VOIDED:
                # A voided run still records its oracle result, but enters
                # no score.
                _fill_workspace_result(deps, fixture, seq, record, with_oracle=True)
            return
        _fill_observation(
            deps,
            protocol,
            arm,
            fixture,
            seq,
            record,
            result,
            recording,
            evidence,
            review_state,
            claim_records,
            ledger,
            attempt_id,
        )
        _apply_oracle_access_check(
            record, transcript_path, signals or oracle_access_signals(protocol)
        )
    finally:
        ledger.close()


def _fill_observation(
    deps: ExperimentDeps,
    protocol: ExperimentProtocol,
    arm: ProtocolArm,
    fixture: ProtocolFixture,
    seq: int,
    record: dict[str, Any],
    result: AttemptResult,
    recording: RecordingModel,
    evidence: CriterionEvidence,
    review_state: ReviewSnapshotState,
    claim_records: list[tuple[str, ClaimRecord]],
    ledger: RunLedger,
    attempt_id: str,
) -> None:
    log = recording.log
    surfaces = [tools for _, tools in log.surfaces]
    surface = surfaces[0] if surfaces else []
    surface_names = [tool.name for tool in surface]
    record["pins"]["tool_schema_digest"] = tool_schema_digest(surface)

    criteria_ids = _criteria_ids(
        deps.tracker.fetch_issue(fixture.issue_number).body
    )
    accepted: dict[str, list[str] | None] | None = None
    force_accepted = False
    for _, claim_record in claim_records:
        raw = claim_record.accepted_raw
        if raw is not None:
            accepted = {
                key: (list(value) if value is not None else None)
                for key, value in normalize_claim(
                    dict(raw), criteria_ids
                ).items()
            }
            force_accepted = claim_record.force_accepted
    record["behaviour"]["activation_trace"] = list(log.activation_trace)
    record["behaviour"]["claim"] = accepted
    record["behaviour"]["claim_calls"] = len(log.report_completion_calls)
    record["behaviour"]["claim_force_accepted"] = force_accepted
    record["behaviour"]["terminal_outcome"] = result.outcome
    record["behaviour"]["terminal_reason"] = result.reason
    implementation_outcome, implementation_reason = implementation_outcome_for(
        result.outcome, result.reason
    )
    record["behaviour"]["implementation_outcome"] = implementation_outcome
    record["behaviour"]["implementation_reason"] = implementation_reason
    record["behaviour"]["criterion_evidence"] = [
        {"test_id": entry.test_id, "passed": entry.passed}
        for entry in evidence.history()
    ]

    usage, elapsed = _ledger_usage(ledger, attempt_id)
    record["meters"]["model_calls"] = log.invocations
    record["meters"]["tool_calls"] = _optional_int(usage, "tool_calls")
    record["meters"]["input_tokens"] = _optional_int(usage, "input_tokens")
    record["meters"]["output_tokens"] = _optional_int(usage, "output_tokens")
    record["meters"]["cache_read_tokens"] = _optional_int(usage, "cache_read_tokens")
    record["meters"]["cache_write_tokens"] = _optional_int(
        usage, "cache_write_tokens"
    )
    record["meters"]["cost_usd"] = _optional_float(usage, "cost_usd")
    record["meters"]["elapsed_seconds"] = elapsed

    review_ledger = _review_from_ledger(ledger, attempt_id)
    if review_ledger is not None:
        ran, status = review_ledger
        provenance = "run-ledger review_status event"
    else:
        ran = False
        status = review_state.status
        provenance = "attempt review state (no ledger review_status event)"
    record["behaviour"]["review"] = {
        "ran": ran,
        "status": status,
        "provenance": provenance,
    }

    _fill_workspace_result(deps, fixture, seq, record, with_oracle=True)


def _fill_workspace_result(
    deps: ExperimentDeps,
    fixture: ProtocolFixture,
    seq: int,
    record: dict[str, Any],
    *,
    with_oracle: bool,
) -> None:
    snapshots_dir = deps.out_dir / "snapshots"
    record["result"]["working_tree_digest"] = tree_digest(deps.workspace)
    diff_text, is_empty = tree_diff(deps.workspace, fixture.base_revision)
    record["result"]["working_tree_diff"] = diff_text
    record["result"]["working_tree_diff_empty"] = is_empty
    if not with_oracle:
        return
    snapshot_dir = snapshots_dir / f"run-{seq:03d}"
    copy_tree_without_git(deps.workspace, snapshot_dir)
    for name, content in fixture.oracle_files.items():
        destination = snapshot_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    oracle = run_oracle(
        fixture.oracle, snapshot_dir, deps.oracle_timeout_seconds
    )
    record["result"]["oracle"] = {
        "command": list(fixture.oracle),
        "exit_code": oracle.exit_code,
        "stdout": oracle.stdout,
        "stderr": oracle.stderr,
        "timed_out": oracle.timed_out,
    }
