"""The operator control listener: reads and durable commands over a Unix socket.

Contract: adoption step 4 of the operator contract (#245, #248, #249,
#252). An
in-process thread started right after the operator store opens. It listens
on a Unix socket in a 0700 directory with the socket at 0600, writable
only by ``agent``, on a tmpfs mount, and serves each connection on its own
thread. It reads only the prepared live view and its own reads of the
store and ledger — never the Worker thread's connection, git, the network
or the provider. A SIGTERM shutdown keeps serving reads until the process
exits: the listener stops only when the Worker exits.

Wire format (private): newline-delimited JSON. The client sends
``{"op": "identity"}``, ``{"op": "status"}``,
``{"op": "stop", "request_id": ..., "expect_repo": ...}``,
``{"op": "resume", "request_id": ..., "expect_repo": ...}`` or
``{"op": "command", "request_id": ...}`` followed by ``\\n``; the
engine answers one JSON object followed by ``\\n``. Anything else —
unparseable bytes, a non-dict, a dict missing its required fields — is a
malformed engine answer and the caller reports ``internal``.

Serialisation (#249): every mutation opens its own connection with a 10 s
busy timeout and commits inside one ``BEGIN IMMEDIATE`` write transaction,
so it serialises with the Claim boundary and with finalization through
SQLite's write lock. A mutation that cannot commit within 10 s answers
``internal`` with ``stored: false``.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coding_agent.operator.live import LiveView

if TYPE_CHECKING:
    from coding_agent.tracker.port import TrackerPort
    from coding_agent.worker.claim import ClaimConfig
from coding_agent.operator.paths import (
    clear_current_socket,
    set_current_socket,
)
from coding_agent.runtime.redaction import Redactable
from coding_agent.operator.store import OPERATOR_BACKEND, agent_version

_LISTEN_BACKLOG = 32
_ACCEPT_TIMEOUT_SECONDS = 0.5
_CONNECTION_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BYTES = 64 * 1024

_MUTATION_TIMEOUT_SECONDS = 10.0
"""Busy timeout for one mutation connection (#249, §8.4).

The listener and the Worker thread each use their own connection;
serialisation is SQLite's write lock, and this timeout keeps a mutation
inside the 10 s budget: where the write lock cannot be won in time the
mutation answers ``internal`` with ``stored: false``.
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_instance_row(ledger_path: Path) -> dict[str, str] | None:
    """This start's instance metadata through a fresh connection.

    A new connection per request: the listener never touches the Worker
    thread's handles, and a blocked Worker-side write cannot stall the
    read past SQLite's own locking (WAL readers do not block writers).
    ``None`` where the file or row does not exist yet (early startup).
    """
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT target_repository, backend, agent_version,"
                " process_started_at FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {
            "target_repository": str(row[0]),
            "backend": str(row[1]),
            "agent_version": str(row[2]),
            "process_started_at": str(row[3]),
        }
    finally:
        connection.close()


def _has_unfinished_attempt(ledger_path: Path) -> bool:
    """Whether the ledger holds an Attempt neither finished nor released."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        try:
            row = connection.execute(
                "SELECT 1 FROM attempts"
                " WHERE state NOT IN ('finished', 'released') LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None
    finally:
        connection.close()


def build_identity_result(
    ledger_path: Path, live: LiveView, *, input_type: str = "issue"
) -> dict[str, Any]:
    """The ``identity`` result: backend, version, contract, repo, mode, clocks."""
    stored = _read_instance_row(ledger_path)
    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    process_started_at = live.process_started_at_iso()
    from coding_agent.operator.step_projection import capabilities_for

    result: dict[str, Any] = {
        "backend": backend,
        "agent_version": version,
        "version": version,
        "contract": "1.0",
        "repository": repository,
        "target_repository": repository,
        "mode": "worker",
        "process_started_at": process_started_at,
        "capabilities": capabilities_for(input_type),
    }
    return result


def _read_intake_detail(ledger_path: Path) -> dict[str, Any]:
    """The durable Intake state plus the pending stop, through a fresh connection."""
    from coding_agent.operator.commands import open_ledger_connection, read_intake

    try:
        connection = open_ledger_connection(ledger_path, timeout_seconds=2.0)
    except sqlite3.Error:
        return {"state": "open", "pending_stop": None, "stop_plan": None}
    try:
        try:
            return read_intake(connection)
        except sqlite3.Error:
            return {"state": "open", "pending_stop": None, "stop_plan": None}
    finally:
        connection.close()


def _read_recent_commands(ledger_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` commands, newest last, through a fresh connection."""
    from coding_agent.operator.commands import list_recent_commands, open_ledger_connection

    try:
        connection = open_ledger_connection(ledger_path, timeout_seconds=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            return [record.to_dict() for record in list_recent_commands(connection, limit)]
        except sqlite3.Error:
            return []
    finally:
        connection.close()


def _read_last_halt(ledger_path: Path) -> dict[str, Any] | None:
    """The durable Halt, if any (survives restarts as ``instance.last_halt``)."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT reason, detail, occurred_at FROM operator_halt"
                " WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        occurred = str(row[2])
        return {
            "reason": str(row[0]),
            "detail": str(row[1]),
            "occurred_at": occurred,
            "time": occurred,
        }
    finally:
        connection.close()


def _read_recent_events(
    ledger_path: Path, process_started_at: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The newest ``limit`` Contract Events of this process, oldest first."""
    try:
        from coding_agent.operator import events as contract_events

        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            return contract_events.list_recent_contract_events(
                connection,
                process_started_at=process_started_at,
                limit=limit,
            )
        except sqlite3.Error:
            return []
    finally:
        connection.close()


_STATUS_SNAPSHOT_LIMIT_BYTES = 256 * 1024
"""The Status Snapshot cap: the serialized ``status`` never exceeds 256 KiB (#253).

Trimming drops entries in contract order (``recent_events`` first, then
``recent_commands``, oldest first) and marks the snapshot ``truncated``.
"""

_UNAVAILABLE = "unavailable"
"""A figure the ledger or live view could carry but does not yet (#253).

Unknown values are never reported as zero: before the first usage flush the
Attempt simply has no spend yet.
"""

_NOT_APPLICABLE = "not_applicable"
"""A figure that cannot apply to this runtime path (#253).

True server-side context occupancy is measurable only inside the provider
SDK — the Run Ledger carries no per-flush context size — so the context
figures report this instead of an invented number.
"""


def _int_or_unknown(value: Any) -> int | str:
    """``value`` where it is an integer meter, else ``unavailable``."""
    if isinstance(value, bool):
        return _UNAVAILABLE
    return value if isinstance(value, int) else _UNAVAILABLE


def _float_or_unknown(value: Any) -> float | str:
    """``value`` where it is a numeric meter, else ``unavailable``."""
    if isinstance(value, bool):
        return _UNAVAILABLE
    if isinstance(value, (int, float)):
        return float(value)
    return _UNAVAILABLE


def _read_attempt_detail(
    ledger_path: Path, attempt_id: str
) -> dict[str, Any] | None:
    """One ``attempts`` row: lifecycle state, pinned config, ledger start."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT attempt_id, state, effective_configuration, created_at"
                " FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            effective = json.loads(str(row[2]))
        except (ValueError, TypeError):
            effective = {}
        if not isinstance(effective, dict):
            effective = {}
        return {
            "attempt_id": str(row[0]),
            "state": str(row[1]),
            "effective_configuration": effective,
            "started_at": str(row[3]),
        }
    finally:
        connection.close()


def _read_latest_usage(ledger_path: Path, attempt_id: str) -> dict[str, Any] | None:
    """The newest ``usage`` record payload for one Attempt, if any."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT payload FROM run_ledger_events"
                " WHERE attempt_id = ? AND kind = 'usage'"
                " ORDER BY id DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            payload = json.loads(str(row[0]))
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None
    finally:
        connection.close()


def _read_missing_usage(ledger_path: Path, attempt_id: str) -> int:
    """How many model responses for one Attempt lacked usage metadata (§11).

    Truthful-usage accounting: the per-Attempt durable counter the runtime
    increments for every model response without ``usage_metadata``. Zero
    where the Attempt is unknown or predates truthful-usage accounting —
    an old row without the counter reads back as complete, never as a
    failure. A fresh connection per request: the listener never touches the
    Worker thread's handles.
    """
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return 0
    try:
        try:
            row = connection.execute(
                "SELECT responses_without_usage FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return 0
        if row is None or row[0] is None:
            return 0
        try:
            count = int(row[0])
        except (TypeError, ValueError):
            return 0
        return max(0, count)
    finally:
        connection.close()


def _missing_usage_reason(count: int) -> str:
    """The Status Snapshot reason for unavailable token and cost figures (§11)."""
    return (
        f"{count} model response(s) lacked usage metadata; "
        "token and cost figures are unavailable"
    )


def _pinned_limits(
    effective_configuration: dict[str, Any],
) -> dict[str, Any]:
    """The four pinned ceilings from the Attempt's Effective Configuration."""
    workflow = effective_configuration.get("workflow")
    limits: Any = workflow.get("limits") if isinstance(workflow, dict) else None
    if not isinstance(limits, dict):
        limits = {}
    wall_clock_minutes = limits.get("wall_clock_minutes")
    wall_clock_seconds: float | str = _UNAVAILABLE
    if isinstance(wall_clock_minutes, bool):
        wall_clock_seconds = _UNAVAILABLE
    elif isinstance(wall_clock_minutes, (int, float)):
        wall_clock_seconds = float(wall_clock_minutes) * 60.0
    return {
        "effective_tokens": limits.get("tokens"),
        "cost_usd": limits.get("cost_usd"),
        "wall_clock_seconds": wall_clock_seconds,
        "tool_calls": limits.get("tool_calls"),
    }


def _build_current_attempt(
    ledger_path: Path, live: LiveView, observed_at: str
) -> dict[str, Any] | None:
    """The running Attempt: identity, phase, budget, ceilings, figures (#253).

    The live view carries the native Attempt id, issue, title, phase and
    activity; the ledger carries the start time, the pinned ceilings and the
    latest usage flush the budget figures match. ``None`` where no Claim
    holds.
    """
    current = live.get_current()
    if current is None:
        return None
    attempt_id = str(current.get("attempt_id") or "")
    from coding_agent.operator.step_projection import is_step_attempt_id

    if is_step_attempt_id(attempt_id):
        return _build_step_current_attempt(
            ledger_path, live, current, attempt_id, observed_at
        )
    detail = _read_attempt_detail(ledger_path, attempt_id)
    started_at = (
        str(detail["started_at"])
        if detail is not None
        else str(current.get("started_at") or "")
    )
    usage_payload = _read_latest_usage(ledger_path, attempt_id) or {}
    raw_usage = usage_payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    missing_usage = _read_missing_usage(ledger_path, attempt_id)
    raw_tokens: int | str = _int_or_unknown(usage.get("tokens"))
    effective_tokens: int | str = _int_or_unknown(usage.get("effective_tokens"))
    cost_used: float | str = _float_or_unknown(usage.get("cost_usd"))
    tool_calls_used = _int_or_unknown(usage.get("tool_calls"))
    elapsed_used = _float_or_unknown(usage_payload.get("elapsed_seconds"))
    missing_reason: str | None = None
    if missing_usage > 0:
        # Truthful-usage accounting (§11): a provider that reports no usage
        # metadata is never reported as a measured zero — the token figures
        # and the cost carry basis ``unavailable`` with a reason. Budget
        # enforcement keeps its current arithmetic; this is a reporting
        # change only.
        missing_reason = _missing_usage_reason(missing_usage)
        raw_tokens = _UNAVAILABLE
        effective_tokens = _UNAVAILABLE
        cost_used = _UNAVAILABLE
    pinned = (
        _pinned_limits(detail["effective_configuration"])
        if detail is not None
        else {}
    )
    configured_effective = pinned.get("effective_tokens", _UNAVAILABLE)
    configured_cost = pinned.get("cost_usd", _UNAVAILABLE)
    configured_wall_clock = pinned.get("wall_clock_seconds", _UNAVAILABLE)
    configured_tool_calls = pinned.get("tool_calls", _UNAVAILABLE)
    cache_read = usage.get("cache_read_tokens")
    cache_input = usage.get("input_tokens")
    if (
        isinstance(cache_read, int)
        and not isinstance(cache_read, bool)
        and isinstance(cache_input, int)
        and not isinstance(cache_input, bool)
        and cache_input > 0
    ):
        cache_hit_rate: float | str = cache_read / cache_input
    else:
        cache_hit_rate = _UNAVAILABLE
    continuations = current.get("continuations")
    review_seats = current.get("review_seats_running")
    budget: dict[str, Any] = {
        "raw_tokens": raw_tokens,
        "effective_tokens": effective_tokens,
        "enforced_tokens": "effective",
    }
    effective_limit: dict[str, Any] = {
        "used": effective_tokens,
        "configured": configured_effective
        if isinstance(configured_effective, (int, float))
        and not isinstance(configured_effective, bool)
        else _UNAVAILABLE,
    }
    cost_limit: dict[str, Any] = {
        "used": cost_used,
        "configured": configured_cost
        if isinstance(configured_cost, (int, float))
        and not isinstance(configured_cost, bool)
        else _UNAVAILABLE,
        "basis": "estimated",
    }
    if missing_reason is not None:
        budget["basis"] = _UNAVAILABLE
        budget["reason"] = missing_reason
        effective_limit["basis"] = _UNAVAILABLE
        effective_limit["reason"] = missing_reason
        cost_limit["basis"] = _UNAVAILABLE
        cost_limit["reason"] = missing_reason
    return {
        "attempt_id": attempt_id,
        "issue_number": current.get("issue_number"),
        "issue_title": current.get("issue_title"),
        "phase": current.get("phase"),
        "started_at": started_at,
        "last_activity_at": current.get("last_activity_at"),
        "observed_at": observed_at,
        "budget": budget,
        "responses_without_usage": missing_usage,
        "limits": {
            "effective_tokens": effective_limit,
            "cost_usd": cost_limit,
            "wall_clock_seconds": {
                "used": elapsed_used,
                "configured": configured_wall_clock
                if isinstance(configured_wall_clock, (int, float))
                and not isinstance(configured_wall_clock, bool)
                else _UNAVAILABLE,
            },
            "tool_calls": {
                "used": tool_calls_used,
                "configured": configured_tool_calls
                if isinstance(configured_tool_calls, (int, float))
                and not isinstance(configured_tool_calls, bool)
                else _UNAVAILABLE,
            },
        },
        "tool_calls": tool_calls_used,
        "elapsed_seconds": elapsed_used,
        "context_used": _NOT_APPLICABLE,
        "context_peak": _NOT_APPLICABLE,
        "context_window": _NOT_APPLICABLE,
        "continuations": continuations
        if isinstance(continuations, int) and not isinstance(continuations, bool)
        else _UNAVAILABLE,
        "review_seats_running": review_seats
        if isinstance(review_seats, int) and not isinstance(review_seats, bool)
        else _UNAVAILABLE,
        "cache_hit_rate": cache_hit_rate,
    }


def _build_step_current_attempt(
    ledger_path: Path,
    live: LiveView,
    current: dict[str, Any],
    attempt_id: str,
    observed_at: str,
) -> dict[str, Any]:
    """The running step-workflow Attempt: native phase and metered budget (S9, §19).

    The MVP shape is kept key-for-key so status consumers keep working: the
    live view still carries identity and activity, but the phase is the
    native ``<step>#<seq>`` and the figures come from the Step Executions
    against the Attempt Ceiling instead of the usage flush. ``None`` usage
    reads as ``unavailable``, never zero.
    """
    from coding_agent.operator import step_projection as projection
    from coding_agent.operator.attempts import split_attempt_id

    try:
        workflow_id, input_number, attempt_number = split_attempt_id(attempt_id)
    except ValueError:
        workflow_id = None
        input_number = current.get("issue_number")
        attempt_number = None
    executions = _read_step_executions(ledger_path, attempt_id)
    detail = _read_attempt_detail(ledger_path, attempt_id)
    started_at = (
        str(detail["started_at"])
        if detail is not None
        else str(current.get("started_at") or "")
    )
    effective = detail["effective_configuration"] if detail is not None else {}
    ceiling = projection.ceiling_from_effective_configuration(effective)
    spend = projection.spend_from_executions(executions)
    budget = projection.budget_for(ceiling=ceiling, spend=spend)
    live_phase = current.get("phase")
    phase = (
        str(live_phase)
        if isinstance(live_phase, str) and live_phase
        else projection.native_phase_for(executions)
    )
    effective_used = budget["effective_tokens"]["used"]
    tool_calls_used = budget["tool_calls"]["used"]
    elapsed_used = budget["wall_clock_seconds"]["used"]
    continuations = current.get("continuations")
    review_seats = current.get("review_seats_running")
    return {
        "attempt_id": attempt_id,
        "workflow_id": workflow_id,
        "input_number": input_number,
        "attempt_number": attempt_number,
        "issue_number": current.get("issue_number"),
        "issue_title": current.get("issue_title"),
        "phase": phase,
        "started_at": started_at,
        "last_activity_at": current.get("last_activity_at"),
        "observed_at": observed_at,
        "budget": {
            "raw_tokens": _UNAVAILABLE,
            "effective_tokens": effective_used,
            "enforced_tokens": "effective",
        },
        "responses_without_usage": 0,
        "limits": {
            "effective_tokens": budget["effective_tokens"],
            "cost_usd": budget["cost_usd"],
            "wall_clock_seconds": budget["wall_clock_seconds"],
            "tool_calls": budget["tool_calls"],
        },
        "tool_calls": tool_calls_used,
        "elapsed_seconds": elapsed_used,
        "context_used": _NOT_APPLICABLE,
        "context_peak": _NOT_APPLICABLE,
        "context_window": _NOT_APPLICABLE,
        "continuations": continuations
        if isinstance(continuations, int) and not isinstance(continuations, bool)
        else _UNAVAILABLE,
        "review_seats_running": review_seats
        if isinstance(review_seats, int) and not isinstance(review_seats, bool)
        else _UNAVAILABLE,
        "cache_hit_rate": _UNAVAILABLE,
    }


def _read_outcome(ledger_path: Path, attempt_id: str) -> tuple[str, str] | None:
    """The recorded Terminal Outcome and reason, if any."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = connection.execute(
                "SELECT outcome, reason FROM outcomes WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return (str(row[0]), str(row[1]))
    finally:
        connection.close()


def _read_effect_rows(ledger_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    """Every effect row (intent or receipt) for one Attempt."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT write_kind, status, remote_id, payload FROM effects"
                " WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        effects: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row[3]))
            except (ValueError, TypeError):
                payload = {}
            effects.append(
                {
                    "write_kind": str(row[0]),
                    "status": str(row[1]),
                    "remote_id": str(row[2]) if row[2] is not None else None,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return effects
    finally:
        connection.close()


def _delivered_pull_request(
    effects: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The delivered pull request from the ``open-pull-request`` receipt."""
    for effect in effects:
        if effect.get("write_kind") != "open-pull-request":
            continue
        if effect.get("status") != "receipt":
            continue
        payload = effect.get("payload")
        if not isinstance(payload, dict):
            continue
        number = payload.get("pull_request_number")
        url = payload.get("pull_request_url")
        if isinstance(number, bool):
            number = None
        if not isinstance(number, int) and number is not None:
            try:
                number = int(str(number))
            except (ValueError, TypeError):
                number = None
        return {
            "number": number,
            "url": str(url) if url is not None else None,
        }
    return None


def _has_pending_effects(effects: list[dict[str, Any]]) -> bool:
    """Whether an intent still waits for its receipt."""
    receipt_kinds = {
        str(effect.get("write_kind"))
        for effect in effects
        if effect.get("status") == "receipt"
    }
    return any(
        effect.get("status") == "intent"
        and str(effect.get("write_kind")) not in receipt_kinds
        for effect in effects
    )


def _parse_json_object(value: Any) -> dict[str, Any] | None:
    """``value`` parsed as a JSON object, or ``None`` where it is not one."""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_step_executions(ledger_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    """Every Step Execution row for one Attempt, in open order (S9, §13.2)."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT step_id, seq, state, result, payload, facts, usage,"
                " limit_fact, entry_from_step, entry_result"
                " FROM step_executions WHERE attempt_id = ? ORDER BY rowid",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        executions: list[dict[str, Any]] = []
        for row in rows:
            executions.append(
                {
                    "step_id": str(row[0]),
                    "seq": int(row[1]),
                    "state": str(row[2]),
                    "result": str(row[3]) if row[3] is not None else None,
                    "payload": _parse_json_object(row[4]),
                    "facts": _parse_json_object(row[5]),
                    "usage": _parse_json_object(row[6]),
                    "limit_fact": _parse_json_object(row[7]),
                    "entry_from_step": str(row[8]) if row[8] is not None else None,
                    "entry_result": str(row[9]) if row[9] is not None else None,
                }
            )
        return executions
    finally:
        connection.close()


def _read_step_effect_dicts(
    ledger_path: Path, attempt_id: str
) -> list[dict[str, Any]]:
    """Every ``step_effects`` row for one Attempt, in key order (S9, §14.1)."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT step_id, execution_number, ordinal, status, remote_id"
                " FROM step_effects WHERE attempt_id = ?"
                " ORDER BY step_id, execution_number, ordinal",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        effects: list[dict[str, Any]] = []
        for row in rows:
            effects.append(
                {
                    "step_id": str(row[0]),
                    "execution_number": int(row[1]),
                    "ordinal": int(row[2]),
                    "status": str(row[3]),
                    "remote_id": str(row[4]) if row[4] is not None else None,
                }
            )
        return effects
    finally:
        connection.close()


def _read_step_transitions(
    ledger_path: Path, attempt_id: str
) -> list[dict[str, Any]]:
    """Every ``step_transitions`` row for one Attempt, in order (S9, §13.2)."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = connection.execute(
                "SELECT kind, from_step, result, to_step, to_end, reason"
                " FROM step_transitions WHERE attempt_id = ? ORDER BY id",
                (attempt_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        transitions: list[dict[str, Any]] = []
        for row in rows:
            transitions.append(
                {
                    "kind": str(row[0]),
                    "from_step": str(row[1]) if row[1] is not None else None,
                    "result": str(row[2]) if row[2] is not None else None,
                    "to_step": str(row[3]) if row[3] is not None else None,
                    "to_end": str(row[4]) if row[4] is not None else None,
                    "reason": str(row[5]) if row[5] is not None else None,
                }
            )
        return transitions
    finally:
        connection.close()


def _attempt_issue_number(attempt_id: str) -> int | None:
    """The issue number from a native Attempt id ``#<issue>/<n>``."""
    text = attempt_id[1:] if attempt_id.startswith("#") else attempt_id
    head = text.split("/", 1)[0]
    try:
        number = int(head)
    except (ValueError, TypeError):
        return None
    return number if number > 0 else None


def _last_finished_attempt(ledger_path: Path) -> tuple[str, str] | None:
    """The latest finished, released or outcome-recorded-with-outcome Attempt."""
    try:
        connection = sqlite3.connect(str(ledger_path), timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = connection.execute(
                "SELECT attempt_id, state FROM attempts"
                " WHERE state IN ('finished', 'released', 'outcome-recorded')"
                " ORDER BY rowid DESC"
            ).fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            attempt_id = str(row[0])
            state = str(row[1])
            if state in ("finished", "released"):
                return (attempt_id, state)
            try:
                outcome_row = connection.execute(
                    "SELECT 1 FROM outcomes WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
            except sqlite3.Error:
                return None
            if outcome_row is not None:
                return (attempt_id, state)
        return None
    finally:
        connection.close()


def _build_last_finished(
    ledger_path: Path, observed_at: str
) -> dict[str, Any] | None:
    """The last finished Attempt: outcome, pull request, attention (#253).

    ``attention_required`` is true for ``failed`` and ``failed-limit``, and
    for an ``outcome-recorded`` Attempt with pending effects; it is false
    for ``delivered``, for ``abandoned`` and for a ``released`` Claim.
    The token figures and the cost mirror ``current_attempt``: where any
    model response lacked usage metadata they carry basis ``unavailable``
    with a reason instead of a measured zero (§11). Budget enforcement
    keeps its current arithmetic; this is a reporting change only.
    """
    found = _last_finished_attempt(ledger_path)
    if found is None:
        return None
    attempt_id, lifecycle_state = found
    from coding_agent.operator.step_projection import is_step_attempt_id as _is_step

    if _is_step(attempt_id):
        return _build_step_last_finished(
            ledger_path, attempt_id, lifecycle_state, observed_at
        )
    recorded = _read_outcome(ledger_path, attempt_id)
    outcome = recorded[0] if recorded is not None else None
    reason = recorded[1] if recorded is not None else ""
    effects = _read_effect_rows(ledger_path, attempt_id)
    pull_request = _delivered_pull_request(effects)
    detail = _read_attempt_detail(ledger_path, attempt_id)
    usage_payload = _read_latest_usage(ledger_path, attempt_id) or {}
    raw_usage = usage_payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    missing_usage = _read_missing_usage(ledger_path, attempt_id)
    raw_tokens: int | str = _int_or_unknown(usage.get("tokens"))
    effective_tokens: int | str = _int_or_unknown(usage.get("effective_tokens"))
    cost_used: float | str = _float_or_unknown(usage.get("cost_usd"))
    missing_reason: str | None = None
    if missing_usage > 0:
        missing_reason = _missing_usage_reason(missing_usage)
        raw_tokens = _UNAVAILABLE
        effective_tokens = _UNAVAILABLE
        cost_used = _UNAVAILABLE
    pinned = (
        _pinned_limits(detail["effective_configuration"])
        if detail is not None
        else {}
    )
    configured_effective = pinned.get("effective_tokens", _UNAVAILABLE)
    configured_cost = pinned.get("cost_usd", _UNAVAILABLE)
    budget: dict[str, Any] = {
        "raw_tokens": raw_tokens,
        "effective_tokens": effective_tokens,
        "enforced_tokens": "effective",
    }
    effective_limit: dict[str, Any] = {
        "used": effective_tokens,
        "configured": configured_effective
        if isinstance(configured_effective, (int, float))
        and not isinstance(configured_effective, bool)
        else _UNAVAILABLE,
    }
    cost_limit: dict[str, Any] = {
        "used": cost_used,
        "configured": configured_cost
        if isinstance(configured_cost, (int, float))
        and not isinstance(configured_cost, bool)
        else _UNAVAILABLE,
        "basis": "estimated",
    }
    if missing_reason is not None:
        budget["basis"] = _UNAVAILABLE
        budget["reason"] = missing_reason
        effective_limit["basis"] = _UNAVAILABLE
        effective_limit["reason"] = missing_reason
        cost_limit["basis"] = _UNAVAILABLE
        cost_limit["reason"] = missing_reason
    if lifecycle_state == "released":
        attention_required = False
    elif outcome in ("failed", "failed-limit"):
        attention_required = True
    elif outcome in ("delivered", "abandoned"):
        attention_required = False
    elif lifecycle_state == "outcome-recorded" and _has_pending_effects(effects):
        attention_required = True
    else:
        attention_required = False
    if outcome is not None:
        display_label = f"{outcome} ({reason})" if reason else outcome
    else:
        display_label = f"{lifecycle_state} (no recorded outcome)"
    return {
        "attempt_id": attempt_id,
        "issue_number": _attempt_issue_number(attempt_id),
        "lifecycle_state": lifecycle_state,
        "outcome": outcome,
        "reason": reason,
        "display_label": display_label,
        "pull_request": pull_request,
        "attention_required": attention_required,
        "budget": budget,
        "responses_without_usage": missing_usage,
        "limits": {
            "effective_tokens": effective_limit,
            "cost_usd": cost_limit,
        },
        "observed_at": observed_at,
    }


def _trim_status_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized snapshot at 256 KiB, in contract order (#253).

    The unbounded tails trim first — ``recent_events``, then
    ``recent_commands`` — oldest entries first (both lists run oldest
    first). The snapshot that lost anything reports ``truncated: true``.
    """
    if len(json.dumps(result).encode("utf-8")) <= _STATUS_SNAPSHOT_LIMIT_BYTES:
        result["truncated"] = False
        return result
    for key in ("recent_events", "recent_commands"):
        items = result.get(key)
        while (
            isinstance(items, list)
            and items
            and len(json.dumps(result).encode("utf-8"))
            > _STATUS_SNAPSHOT_LIMIT_BYTES
        ):
            del items[0]
    result["truncated"] = True
    return result


def build_status_result(ledger_path: Path, live: LiveView) -> dict[str, Any]:
    """The ``status``: instance state, Intake state, Halt, checks, events.

    Instance states: ``starting`` (before readiness, while startup checks or
    recovery run), ``recovering`` (unfinished Attempts from an earlier
    process are being settled), then the §7.2 states ``idle`` (Intake open,
    nothing active), ``active`` (Intake open, an Attempt active),
    ``draining`` (Intake closed, an Attempt still active) and ``paused``
    (Intake closed, nothing active). Readiness — a state other than
    ``starting`` — is reached only after recovery. While a startup check is
    held, ``startup_check`` names it. ``instance.last_halt`` carries the
    durable Halt, if any. ``recent_events`` holds at most the newest 50
    Contract Events of this process, oldest first, every one redacted and
    capped at 300 characters at write time. ``current_attempt`` joins the
    prepared live view (native id, phase, activity) with the ledger's start
    time, pinned ceilings and latest usage flush; ``last_finished`` projects
    the latest finished Attempt with its outcome, pull request and attention
    flag. The snapshot never exceeds 256 KiB: the unbounded tails trim in
    contract order and the snapshot reports ``truncated``.
    ``recent_commands`` carries at most 20 commands, newest last;
    ``intake.pending_stop`` is the pending ``stop``'s ``CommandRef`` or
    ``None``; ``intake.stop_plan`` carries the ``stop --after`` countdown
    (``request_id``, ``sequence``, ``remaining`` and ``counted``) or
    ``None``.
    """
    now = _utc_now_iso()
    active = _has_unfinished_attempt(ledger_path)
    intake = _read_intake_detail(ledger_path)
    intake_state = str(intake.get("state") or "open")
    if intake_state not in ("open", "closed"):
        intake_state = "open"
    pending = intake.get("pending_stop")
    pending_ref: dict[str, Any] | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        pending_ref = {
            "request_id": str(pending["request_id"]),
            "sequence": int(pending.get("sequence") or 0),
        }
    startup_check = live.get_startup_check()
    raw_plan = intake.get("stop_plan")
    plan_ref: dict[str, Any] | None = None
    if isinstance(raw_plan, dict) and raw_plan.get("request_id"):
        plan_ref = {
            "request_id": str(raw_plan["request_id"]),
            "sequence": int(raw_plan.get("sequence") or 0),
            "remaining": int(raw_plan.get("remaining") or 0),
            "counted": int(raw_plan.get("counted") or 0),
        }
    if not live.is_ready():
        if live.is_recovering():
            instance_state = "recovering"
        else:
            instance_state = "starting"
    elif intake_state == "open":
        instance_state = "active" if active else "idle"
    else:
        instance_state = "draining" if active else "paused"
    last_halt = _read_last_halt(ledger_path)
    recent_events = _read_recent_events(
        ledger_path, live.process_started_at_iso()
    )
    result: dict[str, Any] = {
        "generated_at": now,
        "process_started_at": live.process_started_at_iso(),
        "startup_check": startup_check,
        "instance": {
            "state": instance_state,
            "observed_at": now,
            "last_halt": last_halt,
            "startup_check": startup_check,
        },
        "intake": {
            "state": intake_state,
            "pending_stop": pending_ref,
            "stop_plan": plan_ref,
            "observed_at": now,
        },
        "current_attempt": _build_current_attempt(ledger_path, live, now),
        "last_finished": _build_last_finished(ledger_path, now),
        "recent_commands": _read_recent_commands(ledger_path),
        "recent_events": recent_events,
    }
    return _trim_status_snapshot(result)


def _serve_mutation(
    *,
    ledger_path: Path,
    kind: str,
    request: dict[str, Any],
    registry: Redactable = None,
) -> dict[str, Any] | None:
    """Commit a ``stop`` or ``resume``; ``None`` where the line is malformed.

    Opens a private connection with the 10 s busy timeout and commits in
    one ``BEGIN IMMEDIATE`` transaction. Success answers
    ``{"command": ..., "replayed": bool}``; guard failures answer
    ``{"error_code": ...}`` with the fields the client maps to its
    envelope. A lock wait past the budget answers ``internal`` with
    ``stored: false`` (§8.4).
    """
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    expect_repo = request.get("expect_repo")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(expect_repo, str) or not expect_repo:
        return None
    after: Any = request.get("after", None)
    if kind == "stop" and after is not None:
        from coding_agent.operator.commands import validate_stop_after

        validated_after = validate_stop_after(after)
        if validated_after is None:
            return {"error_code": "invalid_argument"}
        after = validated_after
    elif kind != "stop" and "after" in request:
        return None
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=_MUTATION_TIMEOUT_SECONDS
        )
    except sqlite3.Error:
        return {"error_code": "internal", "stored": False}
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        try:
            instance = connection.execute(
                "SELECT target_repository FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            instance = None
        target_repository = (
            str(instance[0]) if instance is not None and instance[0] else ""
        )
        if not target_repository:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        if kind == "stop":
            record, replayed, error = operator_commands.commit_stop(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                after=after,
                registry=registry,
            )
        else:
            record, replayed, error = operator_commands.commit_resume(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                registry=registry,
            )
        if error is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            code = str(error.get("code") or "internal")
            reply: dict[str, Any] = {"error_code": code}
            if "repository" in error:
                reply["repository"] = error["repository"]
            if code == "internal":
                reply["stored"] = False
            return reply
        assert record is not None
        try:
            connection.commit()
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        return {"command": record.to_dict(), "replayed": replayed}
    except sqlite3.OperationalError:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return {"error_code": "internal", "stored": False}
    except sqlite3.Error:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return {"error_code": "internal", "stored": False}
    finally:
        connection.close()


_NEXT_ELIGIBILITY_TIMEOUT_SECONDS = 5.0
"""Bound for the ``next`` commit-time eligibility read (#252, §8.4).

The listener checks eligibility through its own tracker instance — never
the Worker thread's — with no write lock held, abandoning the read where
it exceeds this timeout. A failed or timed-out read never rejects: the
command commits and eligibility waits for the Claim boundary.
"""


def _validate_next_issue(value: Any) -> int | None:
    """The validated priority issue number, or ``None`` where invalid."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _bounded_next_eligibility(
    tracker_factory: Callable[[], TrackerPort] | None,
    claim_config: ClaimConfig | None,
    issue_number: int,
    timeout_seconds: float,
) -> tuple[bool, str | None]:
    """The commit-time eligibility read: ``(read_ok, reason)`` (#252).

    ``(True, None)`` means issue N may be claimed, ``(True, reason)``
    means it is definitely ineligible, and ``(False, None)`` means the
    read itself failed or timed out — which never rejects. The check
    runs on a daemon thread with its own tracker instance from
    ``tracker_factory`` (never the Worker's), so a hung read cannot
    stall the mutation past ``timeout_seconds``; without a factory or a
    claim configuration the read cannot run and reports failure.
    """
    from coding_agent.worker.claim import next_eligibility_reason

    if tracker_factory is None or claim_config is None:
        return (False, None)
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            tracker = tracker_factory()
            box["reason"] = next_eligibility_reason(
                tracker, claim_config, issue_number
            )
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_run, name="next-eligibility", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive() or "reason" not in box:
        return (False, None)
    reason = box["reason"]
    return (True, reason if isinstance(reason, str) or reason is None else None)


def _serve_next_issue(
    *,
    ledger_path: Path,
    request: dict[str, Any],
    claim_config: ClaimConfig | None = None,
    tracker_factory: Callable[[], TrackerPort] | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    registry: Redactable = None,
) -> dict[str, Any] | None:
    """Commit a ``next issue N`` priority; ``None`` where malformed.

    The repo guard and the idempotency pre-check run first with no write
    lock held; then one bounded eligibility read through the listener's
    own tracker instance decides ``rejected`` (definite ineligibility,
    storing nothing) versus commit (eligible, or a failed read whose
    eligibility waits for the Claim boundary). The commit itself runs in
    one ``BEGIN IMMEDIATE`` transaction that re-checks the idempotency,
    supersedes older accepted priorities and inserts the new row
    (§8.4). A lock wait past the budget answers ``internal`` with
    ``stored: false``.
    """
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    expect_repo = request.get("expect_repo")
    if not isinstance(request_id, str) or not request_id:
        return None
    if not isinstance(expect_repo, str) or not expect_repo:
        return None
    issue_number = _validate_next_issue(request.get("issue"))
    if issue_number is None:
        return {"error_code": "invalid_argument"}
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=_MUTATION_TIMEOUT_SECONDS
        )
    except sqlite3.Error:
        return {"error_code": "internal", "stored": False}
    try:
        try:
            instance = connection.execute(
                "SELECT target_repository FROM operator_instance WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            instance = None
        target_repository = (
            str(instance[0]) if instance is not None and instance[0] else ""
        )
        if not target_repository:
            return {"error_code": "internal", "stored": False}
        if expect_repo.lower() != target_repository.lower():
            return {
                "error_code": "repository_mismatch",
                "repository": target_repository,
            }
        try:
            existing = operator_commands.get_command(connection, request_id)
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        if existing is not None:
            expected = operator_commands.payload_digest(
                operator_commands.KIND_NEXT, {"issue": issue_number}
            )
            if existing.payload_digest != expected:
                return {"error_code": "payload_mismatch"}
            return {"command": existing.to_dict(), "replayed": True}
        read_ok, reason = _bounded_next_eligibility(
            tracker_factory,
            claim_config,
            issue_number,
            eligibility_timeout_seconds,
        )
        if read_ok and reason is not None:
            return {
                "error_code": "rejected",
                "details": {"issue": issue_number, "reason": reason},
            }
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            return {"error_code": "internal", "stored": False}
        try:
            record, replayed, error = operator_commands.commit_next(
                connection,
                request_id=request_id,
                expect_repo=expect_repo,
                target_repository=target_repository,
                issue_number=issue_number,
                registry=registry,
            )
        except sqlite3.OperationalError:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        if error is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            code = str(error.get("code") or "internal")
            reply: dict[str, Any] = {"error_code": code}
            if "repository" in error:
                reply["repository"] = error["repository"]
            if code == "internal":
                reply["stored"] = False
            return reply
        assert record is not None
        try:
            connection.commit()
        except sqlite3.Error:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            return {"error_code": "internal", "stored": False}
        return {"command": record.to_dict(), "replayed": replayed}
    finally:
        connection.close()


def _serve_command_lookup(
    *, ledger_path: Path, request: dict[str, Any]
) -> dict[str, Any] | None:
    """Look up one command by request ID; ``None`` where malformed."""
    from coding_agent.operator import commands as operator_commands

    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    try:
        connection = operator_commands.open_ledger_connection(
            ledger_path, timeout_seconds=2.0
        )
    except sqlite3.Error:
        # A read path: nothing is stored, so no ``stored`` flag applies.
        return {"error_code": "internal"}
    try:
        try:
            record = operator_commands.get_command(connection, request_id)
        except sqlite3.Error:
            return {"error_code": "internal"}
        if record is None:
            return {"error_code": "not_found"}
        return {"command": record.to_dict()}
    finally:
        connection.close()


def _handle_one(
    raw: bytes,
    *,
    ledger_path: Path,
    live: LiveView,
    registry: Redactable = None,
    claim_config: ClaimConfig | None = None,
    tracker_factory: Callable[[], TrackerPort] | None = None,
    eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Answer one request line; ``None`` where the line itself is malformed."""
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    op = request.get("op")
    if op == "identity":
        return build_identity_result(ledger_path, live)
    if op == "status":
        return build_status_result(ledger_path, live)
    if op == "stop":
        return _serve_mutation(
            ledger_path=ledger_path, kind="stop", request=request, registry=registry
        )
    if op == "resume":
        return _serve_mutation(
            ledger_path=ledger_path,
            kind="resume",
            request=request,
            registry=registry,
        )
    if op == "next":
        return _serve_next_issue(
            ledger_path=ledger_path,
            request=request,
            claim_config=claim_config,
            tracker_factory=tracker_factory,
            eligibility_timeout_seconds=eligibility_timeout_seconds,
            registry=registry,
        )
    if op == "command":
        return _serve_command_lookup(ledger_path=ledger_path, request=request)
    return None


class OperatorListener:
    """The Unix-socket listener thread and its per-connection threads."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        socket_path: Path,
        live: LiveView,
        registry: Redactable = None,
        claim_config: ClaimConfig | None = None,
        tracker_factory: Callable[[], TrackerPort] | None = None,
        eligibility_timeout_seconds: float = _NEXT_ELIGIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        """Bind nothing yet; :meth:`start` creates the directory and socket.

        ``claim_config`` and ``tracker_factory`` serve ``next issue N``:
        the commit-time eligibility read runs through a fresh instance
        from the factory — never the Worker thread's tracker — bounded
        by ``eligibility_timeout_seconds``. They may also arrive later
        through :meth:`configure_next_issue` once startup knows the
        Agent Identity; without them the read cannot run and the
        command commits for the Claim boundary to decide.
        """
        self._ledger_path = ledger_path
        self._socket_path = socket_path
        self._live = live
        self._registry = registry
        self._next_issue_source: (
            tuple[ClaimConfig, Callable[[], TrackerPort]] | None
        ) = (
            (claim_config, tracker_factory)
            if claim_config is not None and tracker_factory is not None
            else None
        )
        self._eligibility_timeout_seconds = eligibility_timeout_seconds
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> Path:
        """The socket this listener binds (or would bind)."""
        return self._socket_path

    def configure_next_issue(
        self,
        claim_config: ClaimConfig,
        tracker_factory: Callable[[], TrackerPort],
    ) -> None:
        """Provide the priority eligibility source once startup knows it.

        The factory builds a fresh tracker per commit — never the
        Worker's instance. One atomic attribute store publishes both, so
        connections served mid-startup see either the full pair or no
        pair (and commit for the Claim boundary to decide).
        """
        self._next_issue_source = (claim_config, tracker_factory)

    def start(self) -> None:
        """Create the 0700 directory, bind the 0600 socket, serve threads."""
        directory = self._socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self._socket_path))
            server.listen(_LISTEN_BACKLOG)
            server.settimeout(_ACCEPT_TIMEOUT_SECONDS)
            os.chmod(self._socket_path, 0o600)
        except OSError:
            server.close()
            raise
        with self._lock:
            self._socket = server
        set_current_socket(self._socket_path)
        thread = threading.Thread(
            target=self._serve, name="operator-listener", daemon=True
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Stop accepting; existing connections drain. Idempotent."""
        with self._lock:
            server = self._socket
            self._socket = None
            thread = self._thread
            self._thread = None
        self._stop.set()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        clear_current_socket(self._socket_path)
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _serve(self) -> None:
        """Accept connections until :meth:`stop`; each gets its own thread."""
        while not self._stop.is_set():
            with self._lock:
                server = self._socket
            if server is None:
                return
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name="operator-conn",
                daemon=True,
            )
            worker.start()

    def _serve_connection(self, conn: socket.socket) -> None:
        """Serve one connection: one request line, one answer line."""
        try:
            conn.settimeout(_CONNECTION_TIMEOUT_SECONDS)
            data = b""
            while not data.endswith(b"\n"):
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    return
                except OSError:
                    return
                if not chunk:
                    break
                data += chunk
                if len(data) > _MAX_REQUEST_BYTES + 1:
                    return
            line = data.split(b"\n", 1)[0].strip()
            if not line:
                return
            source = self._next_issue_source
            answer = _handle_one(
                line,
                ledger_path=self._ledger_path,
                live=self._live,
                registry=self._registry,
                claim_config=source[0] if source is not None else None,
                tracker_factory=source[1] if source is not None else None,
                eligibility_timeout_seconds=self._eligibility_timeout_seconds,
            )
            if answer is None:
                # Malformed op: still answer with JSON so the client can
                # classify it as an engine failure (``internal``) rather
                # than a transport one. An empty/invalid shape on read is
                # treated the same way by the client's validation.
                payload = {"malformed": True}
            else:
                payload = answer
            try:
                conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
