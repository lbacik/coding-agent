"""Public operator-contract assembly from a :class:`RunLedgerReader` (#435).

Contract: the operator contract's ``status``, ``identity`` and ``attempts``
shapes. Every function here reads the Run Ledger's lifecycle tables
(``attempts``, ``outcomes``, ``effects``, ``step_executions``,
``step_effects``, ``step_transitions``, ``run_ledger_events``) only through
a :class:`~coding_agent.ledger.reader.RunLedgerReader`, and the operator
tables (``operator_instance``, ``operator_halt``) only through
:mod:`coding_agent.operator.store`'s read side. Intake and Operator Command
reads go through :mod:`coding_agent.operator.commands`, Contract Events
through :mod:`coding_agent.operator.events`. No SQL lives in this module:
the table layout stays in :mod:`coding_agent.ledger.queries`.

The live listener (:func:`build_status`, :func:`build_identity`) and the
Offline Read copy (:func:`build_offline_status`,
:func:`build_offline_identity`, :func:`attempts_page`) share these
builders, so live and offline output cannot drift apart.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from pathlib import Path
from typing import Any

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.operator.contract import (
    IDENTITY_CONTRACT,
    figure,
    to_z,
    utc_now_z,
)
from coding_agent.operator.live import LiveView
from coding_agent.operator.store import (
    OPERATOR_BACKEND,
    agent_version,
    read_instance_repository,
    read_instance_row,
    read_last_halt,
    read_provider_capability_report,
)

_STATUS_SNAPSHOT_LIMIT_BYTES = 256 * 1024
"""The Status Snapshot cap: the serialized ``status`` never exceeds 256 KiB.

Trimming drops entries in contract order (``recent_events`` first, then
``recent_commands``, oldest first) and marks the snapshot ``truncated``.
"""

MAX_LIST_LIMIT = 100
"""``attempts list`` never returns more than this per page."""

_UNAVAILABLE = "unavailable"
"""A figure the ledger or live view could carry but does not yet.

Unknown values are never reported as zero: before the first usage flush the
Attempt simply has no spend yet.
"""

_UPPER_BOUND = "estimated"
"""A figure that includes a worst-case charge (§11.4).

A model response without usage (or with partial usage) settles at its
Admission hold's worst case, so the Attempt's token and cost figures are a
ceiling on the true spend, never a measured value. The contract's figure
basis enum has no ``upper-bound`` value, so worst-case charges report
``estimated`` with the reason. ``unavailable`` is kept for figures the
Attempt has not produced yet; it is never produced for a worst-case charge.
"""

_NOT_APPLICABLE = "not_applicable"
"""A figure that cannot apply to this runtime path.

True server-side context occupancy is measurable only inside the provider
SDK — the Run Ledger carries no per-flush context size — so the context
figures report this instead of an invented number.
"""


class InvalidCursor(ValueError):
    """A ``--before`` cursor that does not decode (cursors are opaque)."""


def encode_before_cursor(last_rowid: int) -> str:
    """The opaque ``--before`` cursor for the page ending at ``last_rowid``."""
    raw = json.dumps({"v": 1, "last": last_rowid}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_before_cursor(cursor: str) -> int:
    """The rowid bound a ``--before`` cursor carries.

    Raises :class:`InvalidCursor` where the cursor does not decode: cursors
    are opaque, so anything unrecognised is rejected rather than guessed.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    if payload.get("v") != 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    last = payload.get("last")
    if isinstance(last, bool) or not isinstance(last, int) or last < 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    return last


def check_limit(limit: int) -> None:
    """Reject a page size outside ``1..100``."""
    if not isinstance(limit, bool) and isinstance(limit, int):
        if 1 <= limit <= MAX_LIST_LIMIT:
            return
    raise ValueError(f"invalid --limit {limit!r}: expected 1..{MAX_LIST_LIMIT}")


def trim_status_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized snapshot at 256 KiB, in contract order.

    The unbounded tails trim first -- ``recent_events``, then
    ``recent_commands`` -- oldest entries first (both lists run oldest
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


def attempt_issue_number(attempt_id: str) -> int | None:
    """The issue number from an Attempt id.

    Accepts the step-workflow ``<workflow>#<issue>/<n>`` (for example
    ``implement#44/1``) and the legacy ``#<issue>/<n>``.
    """
    head = attempt_id.split("/", 1)[0]
    if "#" in head:
        head = head.rsplit("#", 1)[1]
    try:
        number = int(head)
    except (ValueError, TypeError):
        return None
    return number if number > 0 else None


def _issue_ref_for_attempt(attempt_id: str, title: Any = None) -> dict[str, Any]:
    """The ``{number, title}`` issue ref for an Attempt id."""
    number: int | None = None
    try:
        from coding_agent.operator.attempts import split_attempt_id as _split

        _workflow, number, _n = _split(attempt_id)
    except Exception:
        number = attempt_issue_number(attempt_id)
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        number = 1
    resolved_title = title if isinstance(title, str) else None
    return {"number": number, "title": resolved_title}


def _missing_usage_reason(count: int) -> str:
    """The Status Snapshot reason for upper-bound token and cost figures (§11.4)."""
    return (
        f"{count} model response(s) settled at their worst-case bound; "
        "token and cost figures are upper bounds"
    )


def _unreported_cache_write_reason(count: int) -> str:
    """The Status Snapshot reason for a gateway unreported cache write (§11.4)."""
    return (
        f"{count} model response(s) priced an unreported cache write at "
        "max(input, cache-write); token and cost figures are upper bounds"
    )


def _upper_bound_reason(missing_usage: int, unreported_cache_write: int) -> str | None:
    """The combined upper-bound reason, or ``None`` where figures are measured.

    Either worst-case settlement (§11.4, missing or partial usage) or an
    unreported gateway cache write marks the Attempt's token and cost
    figures ``upper-bound``; where both happened the reason names both, in
    that order.
    """
    reasons: list[str] = []
    if missing_usage > 0:
        reasons.append(_missing_usage_reason(missing_usage))
    if unreported_cache_write > 0:
        reasons.append(_unreported_cache_write_reason(unreported_cache_write))
    if not reasons:
        return None
    return " ".join(reasons)


def _figure_tokens(
    value: Any, *, estimated_reason: str | None = None
) -> dict[str, Any]:
    """One ``tokens`` figure: measured where int, estimated where worst-case."""
    if isinstance(value, bool):
        return figure(None, "tokens", "unavailable")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if estimated_reason is not None:
            return figure(value, "tokens", "estimated", estimated_reason)
        return figure(value, "tokens", "measured")
    if estimated_reason is not None:
        if isinstance(value, (int, float)):
            return figure(value, "tokens", "estimated", estimated_reason)
        return figure(None, "tokens", "unavailable")
    return figure(None, "tokens", "unavailable")


def _figure_generic(
    value: Any, unit: str, *, basis: str = "measured", reason: str | None = None
) -> dict[str, Any]:
    """One contract figure, forcing null values to a null basis."""
    if isinstance(value, bool):
        return figure(None, unit, "unavailable")
    if isinstance(value, (int, float)):
        if basis in ("unavailable", "not_applicable"):
            return figure(None, unit, basis)
        if reason is not None:
            return figure(value, unit, basis, reason)
        return figure(value, unit, basis)
    return figure(None, unit, "unavailable")


def _configured_figure(value: Any, unit: str) -> dict[str, Any]:
    """One configured limit figure: configured where numeric, else unavailable."""
    if isinstance(value, bool):
        return figure(None, unit, "unavailable")
    if isinstance(value, (int, float)):
        return figure(value, unit, "configured")
    return figure(None, unit, "unavailable")


def _pinned_limits(
    effective_configuration: dict[str, Any],
) -> dict[str, Any]:
    """The four pinned ceilings from the Attempt's Effective Configuration."""
    workflow = effective_configuration.get("workflow")
    ceiling: Any = workflow.get("ceiling") if isinstance(workflow, dict) else None
    if not isinstance(ceiling, dict):
        ceiling = workflow.get("limits") if isinstance(workflow, dict) else None
    limits: Any = ceiling
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


def _attempt_detail(
    reader: RunLedgerReader, attempt_id: str
) -> dict[str, Any] | None:
    """One ``attempts`` row: lifecycle state, pinned config, ledger start."""
    record = reader.get_attempt(attempt_id)
    if record is None:
        return None
    effective = record.effective_configuration
    if not isinstance(effective, dict):
        effective = {}
    started_raw = reader.get_attempt_created_at(attempt_id)
    return {
        "attempt_id": record.attempt_id,
        "state": record.state,
        "effective_configuration": effective,
        "started_at": str(started_raw) if started_raw is not None else "",
    }


def _effect_dicts(reader: RunLedgerReader, attempt_id: str) -> list[dict[str, Any]]:
    """Every effect row (intent or receipt) for one Attempt, as dicts."""
    effects: list[dict[str, Any]] = []
    for record in reader.list_effects(attempt_id):
        payload = record.payload
        effects.append(
            {
                "write_kind": str(record.write_kind),
                "status": str(record.status),
                "remote_id": str(record.remote_id)
                if record.remote_id is not None
                else None,
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return effects


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


def step_projection_for(
    reader: RunLedgerReader, attempt_id: str
) -> dict[str, Any] | None:
    """The §19 projection for a step-workflow Attempt, or ``None`` unknown.

    Reads the Attempt row, its outcome, Step Executions, step effects,
    transitions and pinned Effective Configuration through ``reader`` and
    projects them through :mod:`coding_agent.operator.step_projection`.
    ``None`` where the Attempt row itself is missing. A released Claim
    carries no outcome and never requires attention.
    """
    from coding_agent.operator import step_projection as projection

    attempt = reader.get_attempt(attempt_id)
    if attempt is None:
        return None
    effective = attempt.effective_configuration
    if not isinstance(effective, dict):
        effective = {}
    outcome_record = reader.get_outcome(attempt_id)
    recorded: tuple[str, str] | None = None
    if outcome_record is not None:
        recorded = (str(outcome_record.outcome), str(outcome_record.reason))
    return projection.project_attempt(
        state=str(attempt.state),
        recorded=recorded,
        executions=reader.list_step_executions(attempt_id),
        step_effects=reader.list_step_effects(attempt_id),
        transitions=reader.list_step_transitions(attempt_id),
        effective_configuration=effective,
    )


def sealed_for_attempt(
    reader: RunLedgerReader, attempt_id: str, *, state_dir: Path | None = None
) -> tuple[bool, str | None]:
    """Sealed state for an Attempt via its archive manifest, if locatable.

    ``state_dir`` overrides the archive root (the ledger copy an Offline
    Read opens lives in a temporary directory, so the caller passes the
    real state directory); by default the ledger's own parent is the
    state directory.
    """
    from coding_agent.operator.attempts import archive_dir_for, sealed_info

    repository = read_instance_repository(reader.connection)
    if repository is None:
        return (False, None)
    try:
        archive = archive_dir_for(
            state_dir if state_dir is not None else reader.path.parent,
            repository,
            attempt_id,
        )
    except ValueError:
        return (False, None)
    try:
        sealed, sealed_at_raw = sealed_info(archive)
    except Exception:
        return (False, None)
    sealed_at = to_z(sealed_at_raw) if sealed_at_raw else None
    if sealed and sealed_at is None:
        return (False, None)
    if not sealed:
        return (False, None)
    assert sealed_at is not None
    return (True, sealed_at)


def delivery_for_attempt(
    reader: RunLedgerReader, attempt_id: str
) -> dict[str, Any] | None:
    """The ``v1.0.4`` delivery: ``{pull_request}`` or ``None``."""
    effects = _effect_dicts(reader, attempt_id)
    pr = _delivered_pull_request(effects)
    if pr is None:
        return None
    number = pr.get("number")
    url = pr.get("url")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return {"pull_request": None}
    if not isinstance(url, str) or not url:
        return {"pull_request": None}
    return {"pull_request": {"number": number, "url": url}}


def outcome_for_attempt(
    reader: RunLedgerReader, attempt_id: str, lifecycle_state: str
) -> dict[str, Any] | None:
    """The ``v1.0.4`` outcome, or ``None`` for a released Claim."""
    if lifecycle_state == "released":
        return None
    recorded = reader.get_outcome(attempt_id)
    if recorded is None:
        return None
    native = str(recorded.outcome)
    reason_raw = recorded.reason
    reason = str(reason_raw) if isinstance(reason_raw, str) and reason_raw else None
    label = f"{native} ({reason})" if reason else str(native)
    return {"native": str(native), "reason": reason, "label": label}


def _attention_for_attempt(
    outcome_native: str | None, lifecycle_state: str, has_pending: bool
) -> bool:
    """Whether an Attempt needs attention in ``v1.0.4`` shape."""
    if lifecycle_state == "released":
        return False
    if outcome_native in ("failed", "failed-limit"):
        return True
    if outcome_native in ("delivered", "abandoned"):
        return False
    if lifecycle_state == "outcome-recorded" and has_pending:
        return True
    return False


def attempt_summary(
    reader: RunLedgerReader,
    attempt_id: str,
    lifecycle_state: str,
    observed_at: str,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """One ``v1.0.4`` AttemptSummary for ``attempt_id``.

    ``state_dir`` overrides the archive root for the sealed lookup (see
    :func:`sealed_for_attempt`); by default the ledger's own parent is
    the state directory.
    """
    from coding_agent.operator.step_projection import is_step_attempt_id

    detail = _attempt_detail(reader, attempt_id)
    started_raw = detail["started_at"] if detail is not None else None
    started_at = to_z(str(started_raw)) if started_raw else observed_at
    if started_at is None:
        started_at = observed_at
    issue_title: Any = None
    proj: dict[str, Any] | None = None
    if is_step_attempt_id(attempt_id):
        proj = step_projection_for(reader, attempt_id)
        if proj is not None:
            title_candidate = proj.get("issue_title")
            if isinstance(title_candidate, str):
                issue_title = title_candidate
    issue_ref = _issue_ref_for_attempt(attempt_id, issue_title)
    if is_step_attempt_id(attempt_id):
        outcome_obj: dict[str, Any] | None = None
        delivery_obj: dict[str, Any] | None = None
        attention = False
        if proj is not None:
            raw_outcome = proj.get("outcome")
            if isinstance(raw_outcome, dict):
                native = str(raw_outcome.get("native") or "")
                reason_val = raw_outcome.get("reason")
                reason = (
                    str(reason_val)
                    if isinstance(reason_val, str) and reason_val
                    else None
                )
                if native:
                    outcome_obj = {
                        "native": native,
                        "reason": reason,
                        "label": f"{native} ({reason})" if reason else native,
                    }
            raw_delivery = proj.get("delivery")
            if isinstance(raw_delivery, dict):
                number = raw_delivery.get("number")
                url = raw_delivery.get("url")
                if (
                    isinstance(number, int)
                    and not isinstance(number, bool)
                    and number >= 1
                    and isinstance(url, str)
                    and url
                ):
                    delivery_obj = {"pull_request": {"number": number, "url": url}}
                else:
                    delivery_obj = {"pull_request": None}
            else:
                delivery_obj = delivery_for_attempt(reader, attempt_id)
            attention = bool(proj.get("attention_required", False))
        else:
            outcome_obj = outcome_for_attempt(reader, attempt_id, lifecycle_state)
            delivery_obj = delivery_for_attempt(reader, attempt_id)
            effects = _effect_dicts(reader, attempt_id)
            attention = _attention_for_attempt(
                outcome_obj["native"] if outcome_obj else None,
                lifecycle_state,
                _has_pending_effects(effects),
            )
    else:
        outcome_obj = outcome_for_attempt(reader, attempt_id, lifecycle_state)
        delivery_obj = delivery_for_attempt(reader, attempt_id)
        if delivery_obj is None:
            delivery_obj = {"pull_request": None}
        effects = _effect_dicts(reader, attempt_id)
        attention = _attention_for_attempt(
            outcome_obj["native"] if outcome_obj else None,
            lifecycle_state,
            _has_pending_effects(effects),
        )
        if delivery_obj is None:
            delivery_obj = {"pull_request": None}
    if delivery_obj is None:
        delivery_obj = {"pull_request": None}
    sealed, sealed_at = sealed_for_attempt(reader, attempt_id, state_dir=state_dir)
    finished_at: str | None = None
    if lifecycle_state in ("finished", "released", "outcome-recorded"):
        if sealed and sealed_at is not None:
            finished_at = sealed_at
        else:
            finished_at = None
    return {
        "attempt_id": attempt_id,
        "issue": issue_ref,
        "started_at": started_at,
        "finished_at": finished_at,
        "lifecycle_state": lifecycle_state,
        "outcome": outcome_obj,
        "delivery": delivery_obj,
        "attention_required": attention,
        "sealed": sealed,
        "sealed_at": sealed_at,
    }


def last_finished(
    reader: RunLedgerReader, observed_at: str, *, state_dir: Path | None = None
) -> dict[str, Any] | None:
    """The last finished Attempt as a ``v1.0.4`` AttemptSummary.

    ``state_dir`` overrides the archive root for the sealed lookup (an
    Offline Read opens a ledger copy, so the copy's parent is not the
    state directory); by default the ledger's own parent is used.
    """
    found = reader.last_finished_attempt()
    if found is None:
        return None
    attempt_id, lifecycle_state = found
    return attempt_summary(
        reader, attempt_id, lifecycle_state, observed_at, state_dir=state_dir
    )


def _budget_for_attempt(
    usage: dict[str, Any],
    usage_payload: dict[str, Any],
    detail: dict[str, Any] | None,
    estimated_reason: str | None,
) -> dict[str, Any]:
    """The ``v1.0.4`` budget for a running Attempt."""
    raw = usage.get("tokens")
    effective = usage.get("effective_tokens")
    raw_fig = _figure_tokens(raw)
    if isinstance(effective, bool):
        effective_fig = figure(None, "tokens", "unavailable")
    elif isinstance(effective, (int, float)):
        if estimated_reason is not None:
            effective_fig = figure(effective, "tokens", "estimated", estimated_reason)
        else:
            effective_fig = figure(effective, "tokens", "measured")
    else:
        effective_fig = figure(None, "tokens", "unavailable")
    pinned = _pinned_limits(detail["effective_configuration"]) if detail is not None else {}
    configured_effective = pinned.get("effective_tokens")
    configured_cost = pinned.get("cost_usd")
    configured_wall = pinned.get("wall_clock_seconds")
    configured_tools = pinned.get("tool_calls")
    cost_raw = usage.get("cost_usd")
    if isinstance(cost_raw, bool) or not isinstance(cost_raw, (int, float)):
        cost_used = figure(None, "usd", "unavailable")
    elif estimated_reason is not None:
        cost_used = figure(float(cost_raw), "usd", "estimated", estimated_reason)
    else:
        cost_used = figure(float(cost_raw), "usd", "estimated")
    elapsed_raw = usage_payload.get("elapsed_seconds")
    elapsed_used = (
        _figure_generic(elapsed_raw, "seconds", basis="measured")
        if isinstance(elapsed_raw, (int, float)) and not isinstance(elapsed_raw, bool)
        else figure(None, "seconds", "unavailable")
    )
    tool_raw = usage.get("tool_calls")
    tool_used = (
        _figure_generic(tool_raw, "count", basis="measured")
        if isinstance(tool_raw, int) and not isinstance(tool_raw, bool)
        else figure(None, "count", "unavailable")
    )
    limits: list[dict[str, Any]] = [
        {
            "name": "effective_tokens",
            "used": effective_fig,
            "limit": _configured_figure(configured_effective, "tokens"),
        },
        {
            "name": "cost_usd",
            "used": cost_used,
            "limit": _configured_figure(configured_cost, "usd"),
        },
        {
            "name": "wall_clock_seconds",
            "used": elapsed_used,
            "limit": _configured_figure(configured_wall, "seconds"),
        },
        {
            "name": "tool_calls",
            "used": tool_used,
            "limit": _configured_figure(configured_tools, "count"),
        },
    ]
    return {
        "raw_tokens": raw_fig,
        "effective_tokens": effective_fig,
        "enforced_tokens": "effective",
        "limits": limits,
    }


def _figures_for_attempt(
    live_current: dict[str, Any] | None,
    usage: dict[str, Any],
    usage_payload: dict[str, Any],
    estimated_reason: str | None,
) -> dict[str, Any]:
    """The ``v1.0.4`` figures map for a running Attempt."""
    tool_raw = usage.get("tool_calls")
    tool_fig = (
        _figure_generic(tool_raw, "count", basis="measured")
        if isinstance(tool_raw, int) and not isinstance(tool_raw, bool)
        else figure(None, "count", "unavailable")
    )
    cache_read = usage.get("cache_read_tokens")
    cache_input = usage.get("input_tokens")
    if (
        isinstance(cache_read, int)
        and not isinstance(cache_read, bool)
        and isinstance(cache_input, int)
        and not isinstance(cache_input, bool)
        and cache_input > 0
    ):
        ratio = cache_read / cache_input
        if 0 <= ratio <= 1:
            cache_fig: dict[str, Any] = figure(ratio, "ratio", "measured")
        else:
            cache_fig = figure(None, "ratio", "unavailable")
    elif estimated_reason is not None:
        cache_fig = figure(None, "ratio", "unavailable", estimated_reason)
    else:
        cache_fig = figure(None, "ratio", "unavailable")
    elapsed_raw = usage_payload.get("elapsed_seconds")
    elapsed_fig = (
        _figure_generic(elapsed_raw, "seconds", basis="measured")
        if isinstance(elapsed_raw, (int, float)) and not isinstance(elapsed_raw, bool)
        else figure(None, "seconds", "unavailable")
    )
    figures: dict[str, Any] = {
        "cost_usd": _figure_generic(
            usage.get("cost_usd"),
            "usd",
            basis="estimated" if estimated_reason is not None else "estimated",
            reason=estimated_reason,
        ),
        "tool_calls": tool_fig,
        "turns": figure(None, "count", "not_applicable"),
        "cache_hit_rate": cache_fig,
        "context_used": figure(None, "tokens", "not_applicable"),
        "context_peak": figure(None, "tokens", "not_applicable"),
        "context_window": figure(None, "tokens", "not_applicable"),
        "elapsed_seconds": elapsed_fig,
    }
    if live_current is not None:
        cont = live_current.get("continuations")
        figures["continuations"] = (
            _figure_generic(cont, "count", basis="measured")
            if isinstance(cont, int) and not isinstance(cont, bool)
            else figure(None, "count", "unavailable")
        )
        seats = live_current.get("review_seats_running")
        figures["review_seats_running"] = (
            _figure_generic(seats, "count", basis="measured")
            if isinstance(seats, int) and not isinstance(seats, bool)
            else figure(None, "count", "unavailable")
        )
    return figures


def build_current_attempt(
    reader: RunLedgerReader, live: LiveView, observed_at: str
) -> dict[str, Any] | None:
    """The running Attempt in ``v1.0.4`` shape, or ``None`` where idle."""
    current = live.get_current()
    if current is None:
        return None
    attempt_id = str(current.get("attempt_id") or "")
    if not attempt_id:
        return None
    detail = _attempt_detail(reader, attempt_id)
    started_raw = detail["started_at"] if detail is not None else current.get("started_at")
    started_at = to_z(str(started_raw)) if started_raw else observed_at
    if started_at is None:
        started_at = observed_at
    last_activity_raw = current.get("last_activity_at")
    last_activity_at = to_z(str(last_activity_raw)) if last_activity_raw else None
    usage_payload = reader.get_latest_usage(attempt_id) or {}
    raw_usage = usage_payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    if not usage:
        # Step-workflow Attempts never record a usage flush in production;
        # their metered spend lives in the Step Executions. Fall back to the
        # summed execution spend so the figures mirror the legacy projection
        # instead of reading ``unavailable`` (§11.4 never reads unavailable
        # where a worst-case charge exists).
        from coding_agent.operator import step_projection as projection

        executions = reader.list_step_executions(attempt_id)
        metered = [
            execution.usage
            for execution in executions
            if isinstance(execution.usage, dict) and execution.usage
        ]
        if metered:
            spend = projection.spend_from_executions(executions)
            usage = {
                "effective_tokens": spend["effective_tokens"],
                "cost_usd": spend["cost_usd"],
                "tool_calls": spend["tool_calls"],
            }
            wall_clock = spend["wall_clock_seconds"]
            usage_payload = (
                {"elapsed_seconds": wall_clock}
                if isinstance(wall_clock, (int, float))
                and not isinstance(wall_clock, bool)
                else {}
            )
    missing_usage = reader.get_responses_without_usage(attempt_id)
    unreported = reader.get_responses_with_unreported_cache_write(attempt_id)
    estimated_reason = _upper_bound_reason(missing_usage, unreported)
    issue_title = current.get("issue_title")
    issue_ref = _issue_ref_for_attempt(attempt_id, issue_title)
    phase_raw = current.get("phase")
    native_phase = str(phase_raw) if isinstance(phase_raw, str) and phase_raw else "claim"
    budget = _budget_for_attempt(usage, usage_payload, detail, estimated_reason)
    figures = _figures_for_attempt(current, usage, usage_payload, estimated_reason)
    return {
        "attempt_id": attempt_id,
        "issue": issue_ref,
        "native_phase": native_phase,
        "started_at": started_at,
        "last_activity_at": last_activity_at,
        "observed_at": observed_at,
        "budget": budget,
        "figures": figures,
    }


def build_identity(
    reader: RunLedgerReader, live: LiveView, *, input_type: str = "issue"
) -> dict[str, Any]:
    """The ``identity`` result: backend, version, contract, repo, mode, clocks."""
    stored = read_instance_row(reader.connection)
    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    process_started_at = to_z(live.process_started_at_iso())
    from coding_agent.operator.step_projection import capabilities_for

    result: dict[str, Any] = {
        "backend": backend,
        "agent_version": version,
        "contract": dict(IDENTITY_CONTRACT),
        "repository": repository,
        "mode": "online",
        "process_started_at": process_started_at,
        "capabilities": sorted(capabilities_for(input_type)),
    }
    return result


def _read_intake_detail(reader: RunLedgerReader) -> dict[str, Any]:
    """The durable Intake state plus pending stop, stop plan and priority.

    ``priority`` is ``{request_id, issue}`` while an accepted ``next-issue``
    command waits for the next Claim boundary, else ``None`` (contract
    ``v1.0.4``).
    """
    from coding_agent.operator.commands import (
        pending_next_priority,
        read_intake,
    )

    try:
        intake = read_intake(reader.connection)
        try:
            intake["priority"] = pending_next_priority(reader.connection)
        except sqlite3.Error:
            intake["priority"] = None
        return intake
    except sqlite3.Error:
        return {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
        }


def _read_recent_commands(
    reader: RunLedgerReader, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest ``limit`` commands, newest last."""
    from coding_agent.operator.commands import list_recent_commands

    try:
        return [
            record.to_dict()
            for record in list_recent_commands(reader.connection, limit)
        ]
    except sqlite3.Error:
        return []


def _read_capability_report(reader: RunLedgerReader) -> dict[str, Any] | None:
    """The latest Provider Capability Report, if any.

    The single home for the ``status.provider_capability`` read: the live
    listener (:func:`build_status`) and the Offline Read copy
    (:func:`build_offline_status`) share it, so live and offline output
    cannot drift apart. ``None`` where no assertion has run yet — which
    matters most after a refused startup, when no Worker is running.
    """
    try:
        return read_provider_capability_report(reader.connection)
    except sqlite3.Error:
        return None


def _read_recent_events(
    reader: RunLedgerReader, process_started_at: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The newest ``limit`` Contract Events of this process, oldest first."""
    try:
        from coding_agent.operator import events as contract_events

        return contract_events.list_recent_contract_events(
            reader.connection,
            process_started_at=process_started_at,
            limit=limit,
        )
    except sqlite3.Error:
        return []


def build_status(reader: RunLedgerReader, live: LiveView) -> dict[str, Any]:
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
    (``request_id``, ``remaining`` and ``counted``) or ``None``;
    ``intake.priority`` carries the accepted ``next-issue`` priority
    (``request_id`` and ``issue``) while one waits for the next Claim
    boundary, else ``None``. The snapshot carries the same ``identity``
    object ``identity`` returns. ``provider_capability`` carries the
    latest Provider Capability Report, or ``None`` where no assertion
    has run yet.
    """
    now = utc_now_z()
    active = reader.has_unfinished_attempt()
    intake = _read_intake_detail(reader)
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
            "remaining": int(raw_plan.get("remaining") or 0),
            "counted": int(raw_plan.get("counted") or 0),
        }
    raw_priority = intake.get("priority")
    priority_ref: dict[str, Any] | None = None
    if (
        isinstance(raw_priority, dict)
        and raw_priority.get("request_id")
        and isinstance(raw_priority.get("issue"), int)
        and not isinstance(raw_priority.get("issue"), bool)
        and int(raw_priority["issue"]) >= 1
    ):
        priority_ref = {
            "request_id": str(raw_priority["request_id"]),
            "issue": int(raw_priority["issue"]),
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
    last_halt = read_last_halt(reader.connection)
    recent_events = _read_recent_events(reader, live.process_started_at_iso())
    result: dict[str, Any] = {
        "generated_at": now,
        "identity": build_identity(reader, live),
        "instance": {
            "state": instance_state,
            "startup_check": startup_check,
            "last_halt": last_halt,
            "observed_at": now,
        },
        "intake": {
            "state": intake_state,
            "pending_stop": pending_ref,
            "stop_plan": plan_ref,
            "priority": priority_ref,
            "observed_at": now,
        },
        "current_attempt": build_current_attempt(reader, live, now),
        "last_finished": last_finished(reader, now),
        "recent_commands": _read_recent_commands(reader),
        "recent_events": recent_events,
        "provider_capability": _read_capability_report(reader),
    }
    return trim_status_snapshot(result)


def summaries_for_page(
    reader: RunLedgerReader,
    page_rows: list[Any],
    observed_at: str,
    *,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """``v1.0.4`` AttemptSummaries for one page's ledger rows, newest first.

    Each row contributes its Attempt id and lifecycle state; the summary
    itself (issue ref, outcome, delivery, sealed state) comes from the
    shared producer. Rows with malformed Attempt ids are skipped rather
    than failing the read. ``state_dir`` overrides the archive root for
    the sealed lookup (an Offline Read opens a ledger copy, so the copy's
    parent is not the state directory).
    """
    from coding_agent.operator.attempts import split_attempt_id

    summaries: list[dict[str, Any]] = []
    for row in page_rows:
        attempt_id = str(row[1])
        lifecycle_state = str(row[2])
        try:
            split_attempt_id(attempt_id)
        except ValueError:
            continue
        summary = attempt_summary(
            reader,
            attempt_id,
            lifecycle_state,
            observed_at,
            state_dir=state_dir,
        )
        if summary is None:
            continue
        summaries.append(summary)
    return summaries


def attempts_page(
    reader: RunLedgerReader,
    *,
    limit: int,
    before: str | None,
    state_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of AttemptSummaries, newest first.

    An Attempt is listed as soon as its Claim opens: the rows come straight
    from the ledger's ``attempts`` table in reverse claim order. ``before``
    is the opaque cursor of the previous page's last row; the page holds
    attempts older than it. Returns ``(attempts, next_cursor)`` where
    ``next_cursor`` is the cursor for the following page, or ``None`` where
    this page is the last (contract ``v1.0.4`` ``attempts-list``). Raises
    :class:`InvalidCursor` for a cursor that does not decode and
    :class:`ValueError` for a ``limit`` outside ``1..100``.
    """
    check_limit(limit)
    bound: int | None = None
    if before is not None:
        bound = decode_before_cursor(before)
    rows = reader.list_attempts_page_rows(limit=limit, bound=bound)
    page_rows = list(rows[:limit])
    observed_at = utc_now_z()
    summaries = summaries_for_page(
        reader, page_rows, observed_at, state_dir=state_dir
    )
    next_cursor: str | None = None
    if len(rows) > limit and page_rows:
        next_cursor = encode_before_cursor(int(page_rows[-1][0]))
    return (summaries, next_cursor)


def offline_identity_from_stored(
    stored: dict[str, str] | None,
) -> dict[str, Any]:
    """The offline ``identity`` result for an instance row (or none).

    Contract ``v1.0.4`` shape: the same document ``identity`` returns,
    with ``mode`` ``offline`` and ``process_started_at`` ``None`` (the
    Workflow Configuration is not mounted). The envelope's ``contract``
    stays the ``"major.minor"`` string; only this nested object uses the
    ``{"major", "minor"}`` shape.
    """
    from coding_agent.agentctl import ADVERTISED_CAPABILITIES

    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    return {
        "backend": backend,
        "agent_version": version,
        "contract": dict(IDENTITY_CONTRACT),
        "repository": repository,
        "mode": "offline",
        "process_started_at": None,
        "capabilities": sorted(ADVERTISED_CAPABILITIES),
    }


def offline_intake_projection(
    intake: dict[str, Any], now: str
) -> dict[str, Any]:
    """The ``v1.0.4`` intake projection for an offline status snapshot."""
    intake_state = str(intake.get("state") or "open")
    if intake_state not in ("open", "closed"):
        intake_state = "open"
    pending = intake.get("pending_stop")
    pending_ref: dict[str, Any] | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        try:
            pending_ref = {
                "request_id": str(pending["request_id"]),
                "sequence": int(pending.get("sequence") or 0),
            }
        except (TypeError, ValueError):
            pending_ref = None
    raw_plan = intake.get("stop_plan")
    plan_ref: dict[str, Any] | None = None
    if isinstance(raw_plan, dict) and raw_plan.get("request_id"):
        try:
            plan_ref = {
                "request_id": str(raw_plan["request_id"]),
                "remaining": int(raw_plan.get("remaining") or 0),
                "counted": int(raw_plan.get("counted") or 0),
            }
        except (TypeError, ValueError):
            plan_ref = None
    raw_priority = intake.get("priority")
    priority_ref: dict[str, Any] | None = None
    if (
        isinstance(raw_priority, dict)
        and raw_priority.get("request_id")
        and isinstance(raw_priority.get("issue"), int)
        and not isinstance(raw_priority.get("issue"), bool)
        and int(raw_priority["issue"]) >= 1
    ):
        priority_ref = {
            "request_id": str(raw_priority["request_id"]),
            "issue": int(raw_priority["issue"]),
        }
    return {
        "state": intake_state,
        "pending_stop": pending_ref,
        "stop_plan": plan_ref,
        "priority": priority_ref,
        "observed_at": now,
    }


def fresh_offline_status(now: str) -> dict[str, Any]:
    """The offline ``status`` for a volume with no recorded instance rows."""
    result: dict[str, Any] = {
        "generated_at": now,
        "identity": offline_identity_from_stored(None),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": None,
            "observed_at": now,
        },
        "intake": {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
            "observed_at": now,
        },
        "current_attempt": None,
        "last_finished": None,
        "recent_commands": [],
        "recent_events": [],
        "provider_capability": None,
    }
    return trim_status_snapshot(result)


def _read_offline_intake(reader: RunLedgerReader) -> dict[str, Any]:
    """The durable Intake state from the ledger copy, open where absent.

    Includes the ``stop_plan`` (``None`` where none is recorded) and the
    ``priority`` (``{request_id, issue}`` while an accepted ``next-issue``
    command waits for the next Claim boundary, else ``None``).
    """
    from coding_agent.operator.commands import pending_next_priority, read_intake

    try:
        intake = read_intake(reader.connection)
        try:
            intake["priority"] = pending_next_priority(reader.connection)
        except sqlite3.Error:
            intake["priority"] = None
        if "stop_plan" not in intake:
            intake["stop_plan"] = None
        return intake
    except sqlite3.Error:
        return {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
        }


def _read_offline_commands(
    reader: RunLedgerReader, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest ``limit`` commands from the copy, newest last."""
    from coding_agent.operator.commands import list_recent_commands

    try:
        return [
            record.to_dict()
            for record in list_recent_commands(reader.connection, limit)
        ]
    except sqlite3.Error:
        return []


def build_offline_status(
    reader: RunLedgerReader, *, state_dir: Path | None = None
) -> dict[str, Any]:
    """The offline ``status`` result: ``stopped`` with the last recorded rows.

    Instance state is always ``stopped``: there is no live process to be
    ``starting`` or ``active``. The snapshot carries the same ``identity``
    object ``identity`` returns (``mode`` ``offline``). Intake,
    ``last_halt``, ``last_finished`` and ``recent_commands`` are read from
    the ledger copy as last recorded; ``current_attempt`` is ``None`` and
    ``recent_events`` is empty (no live process owns events). The snapshot
    is capped at 256 KiB in the same contract order as the live one.

    ``state_dir`` is the real state directory for the sealed lookup (the
    ledger copy lives in a temporary directory, so the copy's own parent
    is not the archive root); where omitted the copy's parent is used.
    ``provider_capability`` carries the last recorded report, or ``None``
    where no assertion has run yet.
    """
    now = utc_now_z()
    stored = read_instance_row(reader.connection)
    intake = _read_offline_intake(reader)
    last_halt = read_last_halt(reader.connection)
    finished = last_finished(reader, now, state_dir=state_dir)
    recent_commands = _read_offline_commands(reader)
    result = {
        "generated_at": now,
        "identity": offline_identity_from_stored(stored),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": last_halt,
            "observed_at": now,
        },
        "intake": offline_intake_projection(intake, now),
        "current_attempt": None,
        "last_finished": finished,
        "recent_commands": recent_commands,
        "recent_events": [],
        "provider_capability": _read_capability_report(reader),
    }
    return trim_status_snapshot(result)
